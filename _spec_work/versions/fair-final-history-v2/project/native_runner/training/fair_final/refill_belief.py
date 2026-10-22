from dataclasses import dataclass

from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import semantic_digest
from .conditioned_plan import HistoryConditionedOpponent
from .history import FairHistory
from .history_belief import FULL_POLICY_FRACTION, HIDDEN_PLAN_PROPOSAL, HistoryBeliefFactory
from .prior_history import PriorHistoryBeliefFactory


@dataclass(frozen=True)
class NativeDrawLayout:
    hand: tuple
    cycle: tuple
    hand_slots: tuple
    cycle_slots: tuple

    @property
    def holes(self):
        return tuple(index for index, card in enumerate(self.hand) if card is None)

    @property
    def logical_hand(self):
        return tuple(sorted(card for card in self.hand if card is not None)) + self.cycle[:len(self.holes)]

    @property
    def logical_cycle(self):
        return self.cycle[len(self.holes):]

    @property
    def native_fields(self):
        return (*self.hand_slots, *self.cycle_slots, *([-2]*(8-len(self.cycle_slots))))


def native_draw_layout(player):
    deck = player["deck"]
    if (len(deck) != 8 or {card["cardId"] for card in deck} != set(HOG_DECK)
            or any(type(card["deckSlot"]) is not int for card in deck)
            or {card["deckSlot"] for card in deck} != set(range(8))):
        raise ValueError("refill layout requires eight distinct base Hog cards and slots")
    mapping = {card["deckSlot"]: card["cardId"] for card in deck}
    hand, cycle = player["hand"], player["cycle"]
    if len(hand) > 4 or len(hand)+len(cycle) != 8:
        raise ValueError("refill layout must conserve all eight cards")
    hand_indices = [card["handIndex"] for card in hand]
    cycle_indices = [card["cycleIndex"] for card in cycle]
    if (any(type(index) is not int or not 0 <= index < 4 for index in hand_indices)
            or len(set(hand_indices)) != len(hand_indices)
            or any(type(index) is not int for index in cycle_indices)
            or sorted(cycle_indices) != list(range(len(cycle)))):
        raise ValueError("refill layout has invalid native queue indices")
    cards = hand + cycle
    if (any(type(card["deckSlot"]) is not int or mapping.get(card["deckSlot"]) != card["cardId"] for card in cards)
            or len({card["deckSlot"] for card in cards}) != 8):
        raise ValueError("refill layout card identity or slot permutation differs")
    hand_cards, hand_slots = [None]*4, [-1]*4
    for card in hand:
        hand_cards[card["handIndex"]] = card["cardId"]
        hand_slots[card["handIndex"]] = card["deckSlot"]
    ordered_cycle = sorted(cycle, key=lambda card: card["cycleIndex"])
    return NativeDrawLayout(tuple(hand_cards), tuple(card["cardId"] for card in ordered_cycle),
        tuple(hand_slots), tuple(card["deckSlot"] for card in ordered_cycle))


def verify_completion_layout(layout, completion, history):
    opponent = 1-history.opening.owner
    logical = list(completion.initial_draw)
    drawn = []
    for event in history.deployments:
        if event.owner != opponent:
            continue
        if event.card_id not in logical[:4]:
            raise ValueError("completion does not explain public card history")
        slot = logical.index(event.card_id)
        drawn.append(logical[4])
        logical[slot] = logical[4]
        logical[4:] = [*logical[5:], event.card_id]
    if tuple(logical) != completion.current_draw:
        raise ValueError("completion logical draw differs from public replay")
    pending = len(layout.holes)
    if (set(layout.logical_hand) != set(logical[:4]) or layout.logical_cycle != tuple(logical[4:])
            or pending > len(drawn) or pending and layout.cycle[:pending] != tuple(drawn[-pending:])):
        raise ValueError("native pending refill and sampled logical draw differ")


class RefillPriorHistoryBeliefFactory(PriorHistoryBeliefFactory):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sampler_sha = semantic_digest(dict(schema="fair-final-refill-prior-history.v3",
            base_sampler_sha=self.sampler_sha, root_draw="native-holes-delayed-fifo.v1",
            hidden_plan=HIDDEN_PLAN_PROPOSAL, full_policy_fraction=FULL_POLICY_FRACTION,
            guidance="public-execution-or-cutoff-backward-log-masses",
            empty_cutoff="sample-full-policy-then-replay", posterior="approximate-proposal-not-exact-bayes",
            compatibility="complete-native-legal-history-and-full-own-fair",
            training_admissible=False))

    def create(self, history, *, seed):
        session = super().create(history, seed=seed)
        session.opponent = HistoryConditionedOpponent(self.opponent, history)
        return session

    def build(self, history, information, *, seed, attempts=4, check_deadline=lambda: None):
        if (self.closed or not isinstance(history, FairHistory)
                or information.owner != history.opening.owner or information.tick != history.root_tick
                or information.sha256 != history.root_fair_sha):
            raise ValueError("refill root differs from typed FAIR history")
        session = HistoryBeliefFactory.build(self, history, information, seed=seed,
            attempts=attempts, check_deadline=check_deadline)
        try:
            raw = session.rollout.runtime.environment.native.observe()
            layouts = {player["owner"]: native_draw_layout(player) for player in raw["players"]}
            if set(layouts) != {0, 1} or len(raw["players"]) != 2 or raw["tick"] != information.tick:
                raise ValueError("refill native root identity differs")
            opponent = layouts[1-information.owner]
            verify_completion_layout(opponent, session.draw_completion, history)
            session.conditioning_manifest.update(native_draw_verified=True,
                native_current_draw=[*opponent.hand, *opponent.cycle],
                refill_holes=list(opponent.holes),
                native_draw_schema="native-holes-delayed-fifo.v1")
            if isinstance(getattr(session, "opponent", None), HistoryConditionedOpponent):
                session.conditioning_manifest["hidden_plan_conditioning"] = dict(session.opponent.profile)
            return session
        except BaseException:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
            raise
