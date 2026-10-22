from copy import deepcopy
from dataclasses import asdict
import random
import time
from types import SimpleNamespace

from ..fair_search.contracts import semantic_digest
from ..fair_search.native import NativeBeliefWorld
from .conditioned_plan import HistoryConditionedOpponent
from .deck_belief import DrawCompletion
from .history import FairHistory
from .history_belief import HistoryReplaySession
from .history_provider import HistorySearchProvider, RecoveringHistoryCursor
from .local_reconditioning import (SettledLocalSearchProvider, UnsupportedLocalBoundary,
    refresh_local_information)
from .refill_belief import RefillPriorHistoryBeliefFactory, native_draw_layout, verify_completion_layout


def advance_completion(completion, deployments, *, owner):
    events = tuple(event for event in deployments if event.owner == owner)
    if not events:
        return completion
    current = list(completion.current_draw)
    for event in events:
        if event.card_id not in current[:4]:
            raise ValueError("incremental completion does not explain public executions")
        slot = current.index(event.card_id)
        current[slot] = current[4]
        current[4:] = [*current[5:], event.card_id]
    manifest = dict(schema="fair-final-incremental-draw-completion.v1",
        origin_manifest=deepcopy(completion.manifest.get("origin_manifest", completion.manifest)),
        previous_completion_commitment=semantic_digest([completion.deck, completion.initial_draw, completion.current_draw]),
        public_suffix_sha=semantic_digest([asdict(event) for event in events]),
        completion_commitment=semantic_digest([completion.deck, completion.initial_draw, tuple(current)]),
        scope="public-cycle-increment-from-independent-anchor", training_admissible=False)
    return DrawCompletion(completion.deck, completion.initial_draw, tuple(current), manifest)


class IncrementalRefillSession:
    def __init__(self, session, history, opponent):
        if not isinstance(session, HistoryReplaySession) or not isinstance(history, FairHistory):
            raise ValueError("incremental refill binding requires a typed independent session")
        self.session, self.draw_history, self.frozen_opponent = session, history, opponent

    def __getattr__(self, name):
        return getattr(self.session, name)

    def sync(self, history, information, *, check_deadline=lambda: None):
        session = self.session
        if session.closed or not isinstance(history, FairHistory) or not history.extends(self.draw_history):
            raise ValueError("incremental refill binding requires unchanged historical prefixes")
        completion = advance_completion(session.draw_completion,
            history.deployments[len(self.draw_history.deployments):], owner=1-session.owner)
        previous_opponent = session.opponent
        if history != self.draw_history:
            session.opponent = HistoryConditionedOpponent(self.frozen_opponent, history)
        try:
            result = session.sync(history, information, check_deadline=check_deadline)
        except BaseException:
            session.opponent = previous_opponent
            raise
        try:
            raw = session.rollout.runtime.environment.native.observe()
            layout = native_draw_layout(next(player for player in raw["players"] if player["owner"] == 1-session.owner))
            verify_completion_layout(layout, completion, history)
            session.draw_completion = completion
            session.conditioning_manifest.update(root_fair_sha=history.root_fair_sha,
                root_tick=history.root_tick, history_sha=history.sha256, native_draw_verified=True,
                native_current_draw=[*layout.hand, *layout.cycle], refill_holes=list(layout.holes),
                completion_manifest=deepcopy(completion.manifest),
                hidden_plan_conditioning=dict(session.opponent.profile))
            self.draw_history = history
            return result
        except BaseException:
            session.close()
            raise

    def close(self):
        self.session.close()


class IncrementalRefillBeliefFactory(RefillPriorHistoryBeliefFactory):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sampler_sha = semantic_digest(dict(schema="fair-final-incremental-refill-anchor.v1",
            base_sampler_sha=self.sampler_sha, hidden_plan="rebind-to-verified-extension-before-sync",
            root_draw="incremental-public-cycle-and-native-refill-check", training_admissible=False))

    def create(self, history, *, seed):
        return IncrementalRefillSession(super().create(history, seed=seed), history, self.opponent)


def unseen_relabeling(completion, history, *, seed):
    if (not isinstance(completion, DrawCompletion) or not isinstance(history, FairHistory)
            or type(seed) is not int or seed < 0 or len(completion.deck) != 8
            or len(set(completion.deck)) != 8
            or sorted(completion.initial_draw) != sorted(completion.deck)
            or sorted(completion.current_draw) != sorted(completion.deck)):
        raise ValueError("refill relabeling requires typed eight-card completion and independent seed")
    public_cards = {event.card_id for event in history.deployments if event.owner == 1-history.opening.owner}
    if not public_cards.issubset(completion.deck):
        raise ValueError("public cards differ from independent completion")
    unseen = sorted(set(completion.deck)-public_cards)
    shuffled = unseen.copy()
    random.Random(seed).shuffle(shuffled)
    mapping = {card: card for card in completion.deck}
    mapping.update(zip(unseen, shuffled))
    initial = tuple(mapping[card] for card in completion.initial_draw)
    current = tuple(mapping[card] for card in completion.current_draw)
    manifest = dict(schema="fair-final-public-fixed-card-relabeling.v1",
        source="independent-belief", actual_state_copied=False,
        public_cards_fixed=sorted(public_cards), unseen_count=len(unseen),
        anchor_completion_sha=semantic_digest([completion.initial_draw, completion.current_draw]),
        seed_commitment=semantic_digest([seed]),
        completion_commitment=semantic_digest([completion.deck, initial, current]),
        scope="anchor-conditional-unseen-permutations-not-full-posterior", training_admissible=False)
    return mapping, DrawCompletion(completion.deck, initial, current, manifest)


def refill_relabel_fields(observation, mapping, *, owner):
    if type(owner) is not int or owner not in (0, 1) or len(observation["players"]) != 2:
        raise ValueError("refill relabeling requires both distinct player identities")
    expected, replacement, installed = [], [], None
    for current_owner in (0, 1):
        players = [player for player in observation["players"] if player["owner"] == current_owner]
        if len(players) != 1:
            raise ValueError("refill relabeling native owner differs")
        player = players[0]
        layout = native_draw_layout(player)
        expected.extend(layout.native_fields)
        if current_owner == owner:
            replacement.extend(layout.native_fields)
            continue
        slots = {card["cardId"]: card["deckSlot"] for card in player["deck"]}
        if set(mapping) != set(slots) or set(mapping.values()) != set(slots):
            raise ValueError("refill relabeling must be a complete card bijection")
        hand = tuple(None if card is None else mapping[card] for card in layout.hand)
        cycle = tuple(mapping[card] for card in layout.cycle)
        replacement.extend((*(-1 if card is None else slots[card] for card in hand),
            *(slots[card] for card in cycle), *([-2]*(8-len(cycle)))))
        installed = hand, cycle
    return [observation["generation"], observation["stateEpoch"], observation["tick"], *expected, *replacement], installed


class RefillDrawReconditioner:
    schema = "fair-final-refill-local-relabeling.v1"

    def __init__(self, factory):
        if not isinstance(factory, IncrementalRefillBeliefFactory) or factory.closed:
            raise ValueError("refill reconditioning requires an open independent incremental factory")
        self.factory = factory
        self.sampler_sha = semantic_digest(dict(schema=self.schema, factory_sha=factory.sampler_sha,
            prior_sha=factory.prior.sha256, boundary="both-ready-no-pending-with-native-holes",
            conditioning="public-card-fixed-bijection-of-unseen-anchor-cards",
            native_timing="preserve-hand-holes-fifo-positions-and-refill-clocks",
            scope="anchor-conditional-unseen-permutations-not-full-posterior", training_admissible=False))

    def apply(self, session, history, information, *, seed, check_deadline=lambda: None):
        if (self.factory.closed or not isinstance(session, IncrementalRefillSession) or session.closed
                or session.history != history or session.draw_history != history
                or session.owner != information.owner or information.tick != history.root_tick
                or information.sha256 != history.root_fair_sha):
            raise ValueError("refill reconditioning root differs from verified independent history")
        manifest = session.conditioning_manifest
        if (manifest.get("source") != "independent-belief" or manifest.get("actual_state_copied") is not False
                or manifest.get("sampler_sha") != self.factory.sampler_sha
                or manifest.get("prior_sha") != self.factory.prior.sha256
                or manifest.get("root_fair_sha") != history.root_fair_sha
                or manifest.get("history_sha") != history.sha256
                or manifest.get("native_draw_verified") is not True
                or not isinstance(session.opponent, HistoryConditionedOpponent)
                or session.opponent.opponent is not self.factory.opponent or session.opponent.history != history):
            raise ValueError("refill reconditioning requires current independent construction evidence")
        rollout, owner = session.rollout, session.owner
        runtime = rollout.runtime
        environment = runtime.environment
        if (runtime.failed or runtime.ended or runtime.truncated or runtime.clock.ready != {0, 1}
                or runtime.clock.pending or runtime.clock.tick != information.tick
                or environment._pending_abilities or any(item.status == "queued" for item in environment._pending)):
            raise UnsupportedLocalBoundary("refill reconditioning requires both players ready with no pending plan")
        check_deadline()
        started = time.monotonic()
        native = environment.native
        original = native.observe()
        anchor_layout = native_draw_layout(next(player for player in original["players"] if player["owner"] == 1-owner))
        verify_completion_layout(anchor_layout, session.draw_completion, history)
        mapping, completion = unseen_relabeling(session.draw_completion, history, seed=seed)
        fields, installed = refill_relabel_fields(original, mapping, owner=owner)
        if original["tick"] != information.tick or rollout.freeze_due_information()[owner].sha256 != information.sha256:
            raise ValueError("refill independent anchor changed before mutation")
        before_view = semantic_digest(runtime.frozen[owner])
        before_clock = runtime.clock.capture()
        own_player = next(player for player in original["players"] if player["owner"] == owner)
        own_layout = native_draw_layout(own_player)
        with rollout.snapshot(self.factory.sidecar_factory) as rollback:
            try:
                check_deadline()
                receipt = native._request("belief-draw-probe " + " ".join(map(str, fields)))
                if (receipt.get("ok") is not True or receipt.get("experimental") is not True
                        or receipt.get("generation") != original["generation"] or receipt.get("tick") != original["tick"]
                        or type(receipt.get("stateEpoch")) is not int or receipt["stateEpoch"] <= original["stateEpoch"]):
                    raise ValueError("refill local draw receipt differs")
                environment._raw = dict(native.observe())
                environment._invalidate_rich_cache()
                NativeBeliefWorld._rebase_native_cursors(SimpleNamespace(runtime=runtime),
                    environment._rich_snapshot_for(environment.raw_observation),
                    SimpleNamespace(generation=receipt["generation"], state_epoch=receipt["stateEpoch"], tick=receipt["tick"]))
                actual = environment.raw_observation
                layouts = {player["owner"]: native_draw_layout(player) for player in actual["players"]}
                if ((layouts[1-owner].hand, layouts[1-owner].cycle) != installed or layouts[owner] != own_layout
                        or layouts[1-owner].holes != anchor_layout.holes):
                    raise ValueError("refill local mutation changed its exact native slot contract")
                verify_completion_layout(layouts[1-owner], completion, history)
                updated = refresh_local_information(rollout)
                if (updated[owner].sha256 != information.sha256 or semantic_digest(runtime.frozen[owner]) != before_view
                        or runtime.clock.capture() != before_clock):
                    raise ValueError("refill local mutation changed own FAIR or controller state")
                check_deadline()
                evidence = dict(schema=self.schema, source="independent-belief", actual_state_copied=False,
                    root_fair_sha=information.sha256, history_sha=history.sha256, owner=owner, root_tick=information.tick,
                    sampler_sha=self.sampler_sha, prior_sha=self.factory.prior.sha256,
                    completion_manifest=deepcopy(completion.manifest), native_draw_verified=True,
                    native_current_draw=[*installed[0], *installed[1]], refill_holes=list(anchor_layout.holes),
                    public_and_own_fair_unchanged=True, controller_unchanged=True, history_ticks_replayed=0,
                    scope="anchor-conditional-unseen-permutations-not-full-posterior", training_admissible=False)
                return dict(manifest=evidence, completion=completion, seconds=time.monotonic()-started,
                    cycle_delta=dict(added_plays=0, order_checks=0))
            except BaseException:
                rollback.restore()
                raise


class RefillLocalSearchProvider(SettledLocalSearchProvider):
    def __init__(self, factory, *, history, anchor_seed, attempts=4, check_deadline=lambda: None):
        self.conditioner = RefillDrawReconditioner(factory)
        HistorySearchProvider.__init__(self, factory, history=history, attempts=attempts, check_deadline=check_deadline)
        self.cursor = RecoveringHistoryCursor(factory, seed=anchor_seed, attempts=attempts, check_deadline=check_deadline)
        self.sampler_sha = semantic_digest(dict(schema="fair-final-refill-local-search.v1",
            conditioner_sha=self.conditioner.sampler_sha, anchor_seed_commitment=semantic_digest([anchor_seed]),
            attempts=attempts, training_admissible=False))
