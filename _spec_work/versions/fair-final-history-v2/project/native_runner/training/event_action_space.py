"""Engine legality and opt-in policy constraints, as sampler-ready masks.

This logic used to live only in the renderer (``tools/demo_event_selfplay.py``);
the RL rollout needs the same thing, so it lives here and both call it.

Legality is keyed by **hand slot**: a placement mask exists only for a specific
card currently in hand. That is one of the reasons the sampler draws the card
first, and why the cell mask travels as a ``[card_count, cell_count]`` stack —
one row per candidate deck row, so the stack can be gathered with the drawn card.

Delay masks use native integer elixir rates and exclude the finalization phase.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import torch
from torch import Tensor

from ..contracts import ActionKind, ActionV1
from ..perspective import PerspectiveTransformV1
from .event_config import EVENT_DELAY_OFFSETS_MS
from .event_delay_constraints import playable_delay_mask
from .event_spell_constraints import LightningTargetConstraintV1, LogTargetConstraintV1, constrain_spell_placement
from .event_scheduling import gameplay_end_tick, policy_actions_open
from .v4.catalog import FIRST_REAL_CARD_VOCAB_ID, CardCatalogV1


@dataclass(slots=True)
class EventLegalActionsV1:
    """Sampler-ready legality for one actor at one decision."""

    # Can ACT be delivered at all? True when some deck row is selectable, which
    # includes a card that is unaffordable right now but affordable within the
    # delay vocabulary — the contract commits to a card and a delay, so that is
    # a deliverable play, not a rule violation.
    act_allowed: bool
    # Affordable *and* placeable at this tick, i.e. the engine's own hand-slot
    # gate. Kept separately because it is what "I could act right now" means.
    playable_now: bool
    # The engine's own "this actor may decide now" bit. False means a play by
    # this actor is still in flight, so nothing is playable no matter how full
    # the elixir bar is — asking it anyway records decisions the ring refuses.
    decision_ready: bool
    card_mask: Tensor      # [card_count] bool: rows the sampler may choose
    cell_masks: Tensor     # [card_count, cell_count] bool: legal MODEL-space cells
    delay_masks: Tensor    # [card_count, delay_bin_count] bool: each row's deliverable delays
    hand_index: Tensor     # [card_count] long: hand slot to play, -1 when illegal
    row_card_id: Tensor    # [card_count] long: deck identity of each row, -1 unknown

    @property
    def legal_rows(self) -> tuple[int, ...]:
        return tuple(int(index) for index in self.card_mask.nonzero(as_tuple=False).flatten())


def row_card_ids(frame, catalog: CardCatalogV1, *, card_count: int, device: torch.device) -> Tensor:
    """Map each own-card row's vocabulary id back to a raw card id.

    ``-1`` marks a row whose vocabulary entry is not a real card (padding or an
    unknown bucket), which can never be played.
    """

    vocab = frame.own_cards.card_vocab_id[0][:card_count].to(device)
    raw = torch.full((card_count,), -1, dtype=torch.long, device=device)
    for index in range(int(vocab.shape[0])):
        position = int(vocab[index]) - FIRST_REAL_CARD_VOCAB_ID
        if 0 <= position < len(catalog.raw_card_ids):
            raw[index] = int(catalog.raw_card_ids[position])
    return raw


def mask_decision_ready(mask) -> bool:
    """Read the engine's decision-window bit, defaulting to True when absent."""

    value = None
    reasons = getattr(mask, "reasons", None)
    if isinstance(reasons, Mapping):
        value = reasons.get("decision_ready")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def event_cell_to_native(cell_index: int, *, owner: int, board_width: int) -> tuple[int, int]:
    """Decode a model cell for native plans, validation, replay and trackers.

    The sampled action itself remains in model coordinates for PPO rescoring.
    """

    model_action = ActionV1.play(owner, 0, (cell_index % board_width, cell_index // board_width))
    native_action = PerspectiveTransformV1(actor_owner=owner).action_to_native(model_action)
    return tuple(native_action.target_grid)


def event_placement_rows_to_model(rows, *, owner: int):
    """Rotate native placement rows with the same transform as the tensorizer."""

    return PerspectiveTransformV1(actor_owner=owner)._rows(rows)


def native_hand_slots(player) -> tuple[tuple[int, int], ...]:
    """Return (native slot, card ID), preserving gaps during a card replacement.

    FAIR observations compact the visible cards into ``hand``; their tuple
    positions are not native slot IDs. Older fixtures without slot metadata
    retain their contiguous-hand convention.
    """
    hand = tuple(int(card) for card in player.hand)
    metadata = getattr(player, "metadata", {})
    slots = metadata.get("hand_slot_by_card")
    if slots is None:
        return tuple(enumerate(hand))
    if not isinstance(slots, Mapping):
        raise ValueError("invalid native hand slot mapping")
    capacity = int(metadata.get("native_hand_capacity", 4))
    result = []
    for card in hand:
        slot = slots.get(str(card), slots.get(card))
        if type(slot) is not int or not 0 <= slot < capacity:
            raise ValueError(f"missing or invalid native slot for hand card {card}")
        result.append((slot, card))
    return tuple(result)


def legal_event_actions(
    environment,
    observation,
    frame,
    owner: int,
    *,
    catalog: CardCatalogV1,
    card_count: int,
    cell_count: int,
    board_width: int,
    board_height: int,
    delay_offset_ms: Sequence[int] = EVENT_DELAY_OFFSETS_MS,
    lightning_constraint: LightningTargetConstraintV1 | None = None,
    log_constraint: LogTargetConstraintV1 | None = None,
    device: torch.device | str | None = None,
) -> EventLegalActionsV1:
    """Intersect engine legality with configured policy placement constraints.

    Returns an all-illegal result rather than raising when the actor simply
    cannot play (no hand, not its turn, no legal placement): WAIT is always a
    legal action, so an empty mask is a valid decision state, not an error.
    """

    resolved_device = torch.device(device) if device is not None else torch.device("cpu")
    card_mask = torch.zeros(card_count, dtype=torch.bool, device=resolved_device)
    cell_masks = torch.zeros(card_count, cell_count, dtype=torch.bool, device=resolved_device)
    delay_masks = torch.zeros(card_count, len(delay_offset_ms), dtype=torch.bool, device=resolved_device)
    hand_index = torch.full((card_count,), -1, dtype=torch.long, device=resolved_device)
    row_card_id = row_card_ids(frame, catalog, card_count=card_count, device=resolved_device)
    horizon_ms = int(delay_offset_ms[-1]) if len(delay_offset_ms) else 0

    player = next((item for item in observation.players if int(item.owner) == int(owner)), None)
    mask = environment.action_mask(owner)
    ready = mask_decision_ready(mask)
    tick = int(getattr(observation, "tick", 0))
    end_tick = gameplay_end_tick(environment)
    if player is None or not player.hand or not policy_actions_open(environment, tick):
        return EventLegalActionsV1(False, False, ready, card_mask, cell_masks, delay_masks, hand_index, row_card_id)

    # The engine reports placements even for a slot it closed for insufficient
    # elixir, which is what makes a deferred commitment expressible: the cell can
    # be validated now while the delay carries the play to when the bar fills.
    elixir = float(mask.reasons.get("effective_elixir", player.elixir_exact))
    multiplier = float(getattr(getattr(observation, "time", None), "elixir_multiplier", 1.0) or 1.0)
    mode = getattr(environment, "mode_timeline", None)
    timeline = mode.timeline if mode is not None else None
    costs = _hand_slot_costs(environment, owner)
    deliverable_by_card: dict[int, tuple[int, list[int], tuple[bool, ...]]] = {}
    playable_now = False
    for slot, card_id in native_hand_slots(player):
        entry = mask.placement_masks.get(str(slot))
        if not isinstance(entry, Mapping):
            continue
        rows = entry.get("row_major")
        if not rows:
            continue
        rows = constrain_spell_placement(
            rows, observation, owner, card_id, lightning_constraint=lightning_constraint, log_constraint=log_constraint
        )
        rows = event_placement_rows_to_model(rows, owner=owner)
        cells = [
            y * board_width + x
            for y, row in enumerate(rows[:board_height])
            for x, value in enumerate(row[:board_width])
            if value
        ]
        if not cells:
            continue
        affordable = slot < len(mask.hand_slots) and bool(mask.hand_slots[slot])
        slot_reason = mask.reasons.get("slots", {}).get(str(slot))
        if not affordable and (slot not in costs or slot_reason not in (None, "insufficient_elixir")):
            continue
        minimum = 0 if affordable else minimum_playable_delay_ms(
            elixir=elixir,
            cheapest_cost=costs.get(slot, 0.0),
            elixir_multiplier=multiplier,
            tick=tick, timeline=timeline, end_tick=end_tick,
        )
        if not affordable and (horizon_ms <= 0 or minimum is None or minimum > horizon_ms):
            # Not affordable now and not reachable inside the delay vocabulary:
            # there is no deliverable action for this card.
            continue
        delays = playable_delay_mask(delay_offset_ms, minimum_delay_ms=minimum,
                                     tick=tick, end_tick=end_tick)
        if not any(delays):
            continue
        playable_now = playable_now or affordable
        deliverable_by_card[int(card_id)] = (int(slot), cells, delays)

    for row in range(min(card_count, int(row_card_id.shape[0]))):
        card_id = int(row_card_id[row])
        if card_id < 0 or not bool(frame.own_cards.mask[0][row]):
            continue
        entry = deliverable_by_card.get(card_id)
        if entry is None:
            continue
        slot, cells, delays = entry
        card_mask[row] = True
        hand_index[row] = slot
        cell_masks[row, torch.tensor(cells, dtype=torch.long, device=resolved_device)] = True
        delay_masks[row] = torch.tensor(delays, dtype=torch.bool, device=resolved_device)

    return EventLegalActionsV1(
        bool(card_mask.any()), playable_now, ready, card_mask, cell_masks, delay_masks, hand_index, row_card_id
    )


def _hand_slot_costs(environment, owner: int) -> dict[int, float]:
    """Own visible costs keyed by native slot, independent of raw list order."""

    try:
        raw_hand = next(
            (item["hand"] for item in environment.raw_observation["players"] if int(item["owner"]) == int(owner)),
            [],
        )
        return {int(card.get("handIndex", index)): float(card["cost"])
                for index, card in enumerate(raw_hand)}
    except Exception:  # noqa: BLE001 - legality must not depend on diagnostics
        return {}


def minimum_playable_delay_ms(
    *,
    elixir: float,
    cheapest_cost: float,
    elixir_multiplier: float = 1.0,
    tick_ms: int = 50,
    tick: int = 0,
    timeline=None,
    end_tick: int = 6000,
) -> int | None:
    """Earliest delay at which the cheapest card in hand becomes affordable.

    A decision taken while the bar is short is not illegal: the event contract
    commits to a card and a *delay*, so the play lands once elixir has accrued.
    This converts "cannot afford anything right now" into the earliest
    deliverable delay, which is what the delay mask is built from.
    """

    if elixir_multiplier <= 0.0 or tick_ms != 50:
        raise ValueError("native elixir requires a positive rate and 50 ms ticks")
    if tick >= end_tick:
        return None
    deficit = max(0, round(float(cheapest_cost) * 10_000) - round(float(elixir) * 10_000))
    if deficit == 0:
        return 0
    cursor = tick
    while cursor < end_tick:
        if timeline is not None:
            rate = 100_000 * tick_ms // timeline.elixir_rate(cursor).full_bar_ms
            boundary = min(end_tick, timeline.next_elixir_boundary(cursor) or end_tick)
        else:
            # Compatibility for fixtures without a mode timeline. Production
            # always supplies the verified native schedule (178 / 357 / 537).
            rate = {1.0: 178, 2.0: 357, 3.0: 537}.get(float(elixir_multiplier))
            if rate is None:
                raise ValueError("unrecognized native elixir multiplier")
            boundary = end_tick
        needed = (deficit + rate - 1) // rate
        if needed <= boundary - cursor:
            delay = ((cursor - tick + needed + 1) // 2) * 100
            return delay if tick + delay // tick_ms < end_tick else None
        deficit -= (boundary - cursor) * rate
        cursor = boundary
    return None

