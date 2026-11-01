"""Versioned SSPI action, hand, and FAIR legality contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math

import torch

from native_runner.contracts import ObservationTier
from native_runner.training.event_action_space import legal_event_actions
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_final.adapter import native_payment_mask
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.v4.catalog import FIRST_REAL_CARD_VOCAB_ID
from native_runner.training.fair_final.visibility import validate_fair_view


ACTION_SCHEMA = "sspi-tagged-action.v1"
HAND_SCHEMA = "sspi-hog-hand-state.v1"
DELAY_MS = (200, 400, 600, 800, 1000)
DELAY_SLOTS = tuple(delay // 100 for delay in DELAY_MS)
DELAY_SLOT_TO_INDEX = {slot: index for index, slot in enumerate(DELAY_SLOTS)}
ABILITY_FEATURE_SCHEMA = "sspi-ability-candidate-features.v1"
ABILITY_FEATURE_DIM = 128


class ActionMode(IntEnum):
    WAIT = 0
    PLAY = 1
    ABILITY = 2


@dataclass(frozen=True, slots=True)
class AbilityCandidate:
    source_entity_id: int
    ability_id: int
    features: tuple[float, ...]

    def __post_init__(self):
        if (type(self.source_entity_id) is not int or self.source_entity_id <= 0
                or type(self.ability_id) is not int or self.ability_id <= 0
                or len(self.features) != ABILITY_FEATURE_DIM
                or any(not math.isfinite(float(value)) for value in self.features)):
            raise ValueError("ability candidate requires stable positive source and ability identities")
        object.__setattr__(self, "features", tuple(float(value) for value in self.features))


@dataclass(frozen=True, slots=True)
class SSPIAction:
    mode: ActionMode
    card_id: int | None = None
    cell: int | None = None
    delay_index: int | None = None
    source_entity_id: int | None = None
    ability_id: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "mode", ActionMode(self.mode))
        if self.mode == ActionMode.WAIT:
            if any(value is not None for value in
                    (self.card_id, self.cell, self.delay_index, self.source_entity_id, self.ability_id)):
                raise ValueError("WAIT has no card, target, delay, or ability fields")
        elif self.mode == ActionMode.PLAY:
            if (type(self.card_id) is not int or self.card_id <= 0 or type(self.cell) is not int
                    or not 0 <= self.cell < 18 * 32 or type(self.delay_index) is not int
                    or not 0 <= self.delay_index < len(DELAY_MS)
                    or self.source_entity_id is not None or self.ability_id is not None):
                raise ValueError("PLAY requires a stable card, board cell, and one of five delay bins")
        elif (type(self.source_entity_id) is not int or self.source_entity_id <= 0
                or type(self.ability_id) is not int or self.ability_id <= 0
                or any(value is not None for value in (self.card_id, self.cell, self.delay_index))):
            raise ValueError("ABILITY requires a source entity and ability identity")

    @classmethod
    def wait(cls) -> "SSPIAction":
        return cls(ActionMode.WAIT)

    @classmethod
    def play(cls, card_id: int, cell: int, delay_index: int) -> "SSPIAction":
        return cls(ActionMode.PLAY, card_id=card_id, cell=cell, delay_index=delay_index)

    @classmethod
    def ability(cls, source_entity_id: int, ability_id: int) -> "SSPIAction":
        return cls(ActionMode.ABILITY, source_entity_id=source_entity_id, ability_id=ability_id)

    @property
    def key(self) -> str:
        if self.mode == ActionMode.WAIT:
            return "wait"
        if self.mode == ActionMode.PLAY:
            return f"play:{self.card_id}:{self.cell}:{self.delay_index}"
        return f"ability:{self.source_entity_id}:{self.ability_id}"


@dataclass(frozen=True, slots=True)
class HandSlot:
    index: int
    card_id: int | None
    deck_slot: int | None
    refill_pending: bool = False
    refill_card_id: int | None = None
    refill_remaining_ticks: int | None = None
    refill_time_known: bool = False

    def __post_init__(self):
        if (type(self.index) is not int or not 0 <= self.index < 4
                or (self.card_id is None) != (self.deck_slot is None)
                or type(self.refill_pending) is not bool or type(self.refill_time_known) is not bool
                or (self.refill_remaining_ticks is not None and
                    (type(self.refill_remaining_ticks) is not int or self.refill_remaining_ticks < 0))
                or self.refill_time_known != (self.refill_remaining_ticks is not None)
                or (self.refill_pending != (self.refill_card_id is not None))
                or self.card_id is not None and self.refill_pending):
            raise ValueError("invalid physical hand slot or refill state")

    @property
    def occupied(self) -> bool:
        return self.card_id is not None


@dataclass(frozen=True, slots=True)
class PendingRefill:
    queue_index: int
    card_id: int
    deck_slot: int
    hand_index: int
    remaining_ticks: int | None = None
    time_known: bool = False

    def __post_init__(self):
        if (type(self.queue_index) is not int or self.queue_index < 0
                or type(self.card_id) is not int or self.card_id <= 0
                or type(self.deck_slot) is not int or not 0 <= self.deck_slot < 8
                or type(self.hand_index) is not int or not 0 <= self.hand_index < 4
                or type(self.time_known) is not bool
                or (self.remaining_ticks is not None and
                    (type(self.remaining_ticks) is not int or self.remaining_ticks < 0))
                or self.time_known != (self.remaining_ticks is not None)):
            raise ValueError("invalid pending native refill record")


@dataclass(frozen=True, slots=True)
class HandState:
    """Stable eight-card identity plus four physical slots and delayed FIFO draws."""

    deck: tuple[tuple[int, int], ...]
    slots: tuple[HandSlot, ...]
    known_next_card_id: int | None
    cycle: tuple[tuple[int, int], ...]
    pending_refills: tuple[PendingRefill, ...]

    @property
    def ready_card_ids(self) -> tuple[int, ...]:
        return tuple(slot.card_id for slot in self.slots if slot.card_id is not None)

    @property
    def ready_hand_count(self) -> int:
        return sum(slot.occupied for slot in self.slots)

    @property
    def next_card_available_now(self) -> bool:
        return False

    @classmethod
    def from_native(cls, player: dict, *, expected_deck=HOG_DECK) -> "HandState":
        def native_int(item, key):
            value = item[key]
            if type(value) is not int:
                raise ValueError(f"native {key} must be an integer")
            return value

        native_deck = tuple((native_int(item, "deckSlot"), native_int(item, "cardId"))
            for item in player["deck"])
        stable_cards = tuple(expected_deck)
        if (len(stable_cards) != 8 or len(set(stable_cards)) != 8
                or len(native_deck) != 8
                or tuple(sorted(slot for slot, _ in native_deck)) != tuple(range(8))
                or len({card for _, card in native_deck}) != 8
                or {card for _, card in native_deck} != set(stable_cards)):
            raise ValueError("native deck identity differs from the fixed eight-card HOG deck")
        native_card_by_slot = dict(native_deck)
        stable_slot_by_card = {card_id: slot for slot, card_id in enumerate(stable_cards)}
        deck = tuple(enumerate(stable_cards))
        hand_by_slot = {}
        for item in player["hand"]:
            slot, card = native_int(item, "handIndex"), native_int(item, "cardId")
            native_deck_slot = native_int(item, "deckSlot")
            if (not 0 <= slot < 4 or slot in hand_by_slot
                    or native_card_by_slot.get(native_deck_slot) != card):
                raise ValueError("native ready hand has duplicate or mismatched physical slot identity")
            hand_by_slot[slot] = (card, stable_slot_by_card[card])
        cycle_items = tuple((native_int(item, "cycleIndex"), native_int(item, "deckSlot"),
            native_int(item, "cardId")) for item in player["cycle"])
        if (tuple(index for index, _, _ in cycle_items) != tuple(range(len(cycle_items)))
                or len({card for _, _, card in cycle_items}) != len(cycle_items)
                or len({deck_slot for _, deck_slot, _ in cycle_items}) != len(cycle_items)
                or any(native_card_by_slot.get(deck_slot) != card for _, deck_slot, card in cycle_items)
                or len(player["hand"]) + len(cycle_items) != 8):
            raise ValueError("native refill queue is not an ordered subset of the stable deck")
        cycle = tuple((stable_slot_by_card[card], card) for _, _, card in cycle_items)
        slots = tuple(HandSlot(index, *hand_by_slot.get(index, (None, None))) for index in range(4))
        next_item = player.get("nextCard")
        known_next = None if next_item is None else native_int(next_item, "cardId")
        if (bool(cycle) != (next_item is not None)
                or cycle_items and (known_next != cycle[0][1]
                    or native_int(next_item, "deckSlot") != cycle_items[0][1])):
            raise ValueError("known next card differs from the native refill FIFO")
        ready = {slot.card_id for slot in slots if slot.card_id is not None}
        if ready.intersection(card for _, card in cycle):
            raise ValueError("a card cannot be ready and pending in the refill queue")
        holes = sum(not slot.occupied for slot in slots)
        if holes > len(cycle):
            raise ValueError("native refill queue cannot satisfy the number of physical hand holes")
        holes_by_slot = [slot.index for slot in slots if not slot.occupied]
        pending = tuple(PendingRefill(index, card, deck_slot, holes_by_slot[index])
            for index, (deck_slot, card) in enumerate(cycle[:holes]))
        pending_by_slot = {item.hand_index: item for item in pending}
        slots = tuple(HandSlot(index, *hand_by_slot.get(index, (None, None)),
            refill_pending=index in pending_by_slot,
            refill_card_id=pending_by_slot[index].card_id if index in pending_by_slot else None)
            for index in range(4))
        if {slot.deck_slot for slot in slots if slot.occupied} | {slot for slot, _ in cycle} != set(range(8)):
            raise ValueError("ready hand and refill cycle do not conserve all stable deck slots")
        return cls(deck, slots, known_next, cycle, pending)


class SSPILegality:
    """Conditional legal prefixes. The ability branch is present and masked for HOG."""

    def __init__(self, card_ids, legal_plays, *, wait=True, ability_candidates=()):
        self.card_ids = tuple(card_ids)
        self.ability_candidates = tuple(ability_candidates)
        if (len(set(self.card_ids)) != len(self.card_ids)
                or any(type(card) is not int or card <= 0 for card in self.card_ids)
                or legal_plays.dtype != torch.bool or legal_plays.ndim != 3
                or legal_plays.shape != (len(self.card_ids), 576, len(DELAY_MS))
                or type(wait) is not bool
                or any(not isinstance(item, AbilityCandidate) for item in self.ability_candidates)):
            raise ValueError("invalid SSPI legal-prefix contract")
        self.plays = legal_plays.clone()
        self.cells = self.plays.any(-1)
        self.cards = self.cells.any(-1)
        self.modes = torch.tensor([wait, bool(self.cards.any()), bool(self.ability_candidates)],
            dtype=torch.bool, device=legal_plays.device)
        if not bool(self.modes.any()):
            raise ValueError("decision has no legal action")
        if not self.ability_candidates and bool(self.modes[ActionMode.ABILITY]):
            raise ValueError("empty ability candidates must mask ABILITY")

    @classmethod
    def from_native(cls, environment, observation, frame, owner, *, catalog,
            hand_state: HandState | None = None) -> "SSPILegality":
        if (observation.tier != ObservationTier.FAIR or observation.owner != owner
                or observation.tick != environment.current_tick):
            raise ValueError("legality requires this owner's current observation")
        validate_fair_view(observation, owner)
        if hand_state is None:
            player = next(item for item in environment.raw_observation["players"] if item["owner"] == owner)
            hand_state = HandState.from_native(player)
        count = frame.own_cards.card_vocab_id.shape[1]
        legacy = legal_event_actions(environment, observation, frame, owner, catalog=catalog,
            card_count=count, cell_count=576, board_width=18, board_height=32,
            delay_offset_ms=tuple(range(0, 3001, 100)))
        if not legacy.decision_ready:
            raise ValueError("legality requested outside a decision boundary")
        ready = set(hand_state.ready_card_ids)
        rows = [row for row in range(count) if bool(frame.own_cards.mask[0, row])
                and int(legacy.row_card_id[row]) in ready]
        card_ids = [int(legacy.row_card_id[row]) for row in rows]
        if set(card_ids) != ready:
            raise ValueError("one or more ready physical hand slots have no semantic action row")
        full = legacy.cell_masks[rows, :, None] & legacy.delay_masks[rows, None, :]
        raw = environment.raw_observation
        player = next(item for item in raw["players"] if int(item["owner"]) == owner)
        costs = {int(card["cardId"]): int(card["cost"]) * 10000 for card in player["hand"]}
        for index, card_id in enumerate(card_ids):
            full[index] &= native_payment_mask(tick=int(raw["tick"]), elixir_raw=int(player["elixirRaw"]),
                cost_raw=costs[card_id], timeline=environment.mode_timeline.timeline)[None, :]
        selected = torch.tensor(DELAY_SLOTS, dtype=torch.long, device=full.device)
        legal_plays = full.index_select(-1, selected)
        return cls(card_ids, legal_plays, ability_candidates=())

    def mask(self, prefix=()):
        prefix = tuple(prefix)
        if not prefix:
            return self.modes
        if prefix == (ActionMode.PLAY,):
            return self.cards
        if prefix == (ActionMode.ABILITY,):
            return torch.ones(len(self.ability_candidates), dtype=torch.bool, device=self.modes.device)
        if len(prefix) == 2 and prefix[0] == ActionMode.PLAY:
            card = int(prefix[1])
            if not 0 <= card < len(self.card_ids) or not bool(self.cards[card]):
                raise ValueError("PLAY card prefix has no legal completion")
            return self.cells[card]
        if len(prefix) == 3 and prefix[0] == ActionMode.PLAY:
            card, cell = int(prefix[1]), int(prefix[2])
            if (not 0 <= card < len(self.card_ids) or not 0 <= cell < 576
                    or not bool(self.cells[card, cell])):
                raise ValueError("PLAY cell prefix has no legal completion")
            return self.plays[card, cell]
        raise ValueError("invalid SSPI action prefix")

    def legal(self, action: SSPIAction) -> bool:
        from ..fair_final.policy import FullAction

        if isinstance(action, FullAction):
            try:
                action = from_full_action(action)
            except (TypeError, ValueError):
                return False
        if not isinstance(action, SSPIAction):
            return False
        if action.mode == ActionMode.WAIT:
            return bool(self.modes[ActionMode.WAIT])
        if action.mode == ActionMode.PLAY:
            if action.card_id not in self.card_ids:
                return False
            card = self.card_ids.index(action.card_id)
            return bool(self.plays[card, action.cell, action.delay_index])
        return any((item.source_entity_id, item.ability_id) ==
            (action.source_entity_id, action.ability_id) for item in self.ability_candidates)

    def delay_ms(self, delay_index: int) -> int:
        if type(delay_index) is not int or not 0 <= delay_index < len(DELAY_MS):
            raise ValueError("delay index outside the fixed SSPI bins")
        return DELAY_MS[delay_index]

    def identity(self):
        return dict(card_ids=self.card_ids, plays=self.plays.detach().cpu().tolist(),
            ability_feature_schema=ABILITY_FEATURE_SCHEMA,
            ability_candidates=[(item.source_entity_id, item.ability_id, item.features)
                for item in self.ability_candidates],
            modes=self.modes.detach().cpu().tolist())


@dataclass(frozen=True)
class SSPIInformation:
    owner: int
    tick: int
    frame: object
    hand: HandState
    legality: SSPILegality

    @property
    def sha256(self):
        if (type(self.owner) is not int or self.owner not in (0, 1)
                or type(self.tick) is not int or self.tick < 0):
            raise ValueError("invalid SSPI information identity")
        return semantic_digest(dict(schema="sspi-fair-information.v1", owner=self.owner, tick=self.tick,
            frame=self.frame, hand=self.hand, legality=self.legality.identity()))


def to_full_action(action: SSPIAction):
    """Translate only executable WAIT/PLAY into the existing native reservation contract."""
    from ..fair_final.policy import FullAction

    if action.mode == ActionMode.WAIT:
        return FullAction()
    if action.mode == ActionMode.PLAY:
        return FullAction(action.card_id, action.cell, DELAY_SLOTS[action.delay_index])
    raise ValueError("ABILITY remains masked in the fixed HOG pilot")


def from_full_action(action) -> SSPIAction:
    from ..fair_final.policy import FullAction

    if not isinstance(action, FullAction):
        raise TypeError("native reservation action must use FullAction")
    if action.card_id is None:
        return SSPIAction.wait()
    delay_index = DELAY_SLOT_TO_INDEX.get(action.delay_slot)
    if delay_index is None:
        raise ValueError("native action delay is outside the five SSPI bins")
    return SSPIAction.play(action.card_id, action.cell, delay_index)


def stable_card_rows(frame, catalog, card_ids, *, index=0):
    positions = {}
    for row, vocab_id in enumerate(frame.own_cards.card_vocab_id[index].tolist()):
        if not bool(frame.own_cards.mask[index, row]):
            continue
        offset = vocab_id - FIRST_REAL_CARD_VOCAB_ID
        if 0 <= offset < len(catalog.raw_card_ids):
            card_id = int(catalog.raw_card_ids[offset])
            if card_id in positions:
                raise ValueError("ambiguous stable card identity in semantic frame")
            positions[card_id] = row
    if any(card_id not in positions for card_id in card_ids):
        raise ValueError("ready card identity is missing from semantic frame")
    return tuple(positions[card_id] for card_id in card_ids)
