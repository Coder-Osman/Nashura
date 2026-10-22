"""Contract tests for the sampler-facing legality masks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import ActionMaskV1
from native_runner.training.event_action_space import (
    legal_event_actions,
    minimum_playable_delay_ms,
    playable_delay_mask,
    native_hand_slots,
)
from native_runner.training.v4.catalog import FIRST_REAL_CARD_VOCAB_ID, CardCatalogV1

BOARD_WIDTH = 18
BOARD_HEIGHT = 32
CARD_COUNT = 8
CELL_COUNT = BOARD_WIDTH * BOARD_HEIGHT


def _catalog() -> CardCatalogV1:
    return CardCatalogV1.from_raw_ids((9004, 1002, 7007, 3003, 5005, 8008, 6006, 2001))


def _deck() -> list[int]:
    return [int(card_id) for card_id in _catalog().raw_card_ids]


def _frame(catalog, rows: list[int], *, masked_out: tuple[int, ...] = ()):
    """Build the own-cards view: one vocabulary id per deck row."""

    vocab = [FIRST_REAL_CARD_VOCAB_ID + catalog.raw_card_ids.index(card_id) for card_id in rows]
    mask = torch.ones(1, CARD_COUNT, dtype=torch.bool)
    for row in masked_out:
        mask[0, row] = False
    return SimpleNamespace(
        own_cards=SimpleNamespace(
            card_vocab_id=torch.tensor([vocab], dtype=torch.long),
            mask=mask,
        )
    )


def _placement(row_major: list[list[bool]]) -> dict:
    return {"row_major": row_major}


def _environment(
    placement_by_slot: dict[str, list[list[bool]]],
    *,
    play_allowed: bool = True,
    hand_slots=(True, True, True, True),
    decision_ready: bool | None = None,
    costs: list[float] | None = None,
):
    reasons = {} if decision_ready is None else {"decision_ready": decision_ready}
    mask = ActionMaskV1(
        kinds={"wait": True, "play_card": play_allowed},
        hand_slots=tuple(hand_slots),
        placement_masks=placement_by_slot,
        reasons=reasons,
    )
    raw = {"players": [{"owner": 0, "hand": [{"cost": cost} for cost in (costs or [])]}]}
    return SimpleNamespace(action_mask=lambda owner: mask, raw_observation=raw)


def _observation(hand: list[int], *, elixir: float = 10.0, multiplier: float = 1.0):
    return SimpleNamespace(
        players=[SimpleNamespace(owner=0, hand=list(hand), elixir_exact=elixir)],
        time=SimpleNamespace(elixir_multiplier=multiplier),
    )


def _grid(cells: list[tuple[int, int]]) -> list[list[bool]]:
    grid = [[False] * BOARD_WIDTH for _ in range(BOARD_HEIGHT)]
    for x, y in cells:
        grid[y][x] = True
    return grid


def test_sparse_hand_uses_native_slots_for_cells_and_affordability():
    catalog = _catalog()
    deck = _deck()
    observation = _observation([deck[0], deck[1], deck[2]], elixir=2.)
    observation.players[0].metadata = dict(
        hand_slot_by_card={str(deck[0]): 0, str(deck[1]): 2, str(deck[2]): 3},
        native_hand_capacity=4)
    environment = _environment({
        "0": _placement(_grid([(1, 1)])),
        "2": _placement(_grid([(2, 2)])),
        "3": _placement(_grid([(3, 3)])),
    }, hand_slots=(True, False, True, False))
    # Deliberately unordered telemetry; slot 3 costs 3 elixir and requires a delay.
    environment.raw_observation["players"][0]["hand"] = [
        {"handIndex": 3, "cost": 3}, {"handIndex": 0, "cost": 1}, {"handIndex": 2, "cost": 2}]
    legal = legal_event_actions(environment, observation, _frame(catalog, deck), 0,
        catalog=catalog, card_count=CARD_COUNT, cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH, board_height=BOARD_HEIGHT, delay_offset_ms=(0, 1000, 3000))
    assert legal.hand_index[:3].tolist() == [0, 2, 3]
    for row, cell in enumerate((19, 38, 57)):
        assert legal.cell_masks[row].nonzero().flatten().tolist() == [cell]
    assert legal.delay_masks[2].tolist() == [False, False, True]
    assert native_hand_slots(observation.players[0]) == ((0, deck[0]), (2, deck[1]), (3, deck[2]))


@pytest.mark.parametrize("slots", [{}, {"123": -1}, {"123": 4}, {"123": True}])
def test_invalid_explicit_hand_mapping_does_not_fall_back_to_tuple_position(slots):
    with pytest.raises(ValueError, match="native slot"):
        native_hand_slots(SimpleNamespace(hand=(123,), metadata={"hand_slot_by_card": slots}))


def test_maps_deck_rows_to_legal_cells_and_hand_slots() -> None:
    catalog = _catalog()
    deck = _deck()[:4] + [_deck()[4]] * 4
    frame = _frame(catalog, deck)
    # Slot 0 holds deck[0]; its only legal placements are (3, 4) and (0, 1).
    environment = _environment({"0": _placement(_grid([(3, 4), (0, 1)]))})
    observation = _observation([deck[0], 0, 0, 0])

    legal = legal_event_actions(
        environment,
        observation,
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )

    assert legal.act_allowed
    assert legal.legal_rows == (0,)
    assert int(legal.hand_index[0]) == 0
    # Row-major flattening must match the model's cell indexing (y * width + x).
    assert sorted(legal.cell_masks[0].nonzero(as_tuple=False).flatten().tolist()) == [
        1 * BOARD_WIDTH + 0,
        4 * BOARD_WIDTH + 3,
    ]
    # Cards in hand but with no legal placement stay illegal, so the sampler can
    # never draw a placement the engine would refuse.
    assert not bool(legal.cell_masks[1].any())


def test_ignores_rows_the_frame_masks_out_and_cards_not_in_hand() -> None:
    catalog = _catalog()
    deck = _deck()
    frame = _frame(catalog, deck, masked_out=(0,))
    environment = _environment({"0": _placement(_grid([(5, 5)]))})
    observation = _observation([deck[0]])

    legal = legal_event_actions(
        environment,
        observation,
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    # Row 0 carries the playable card but the frame says the row is not visible.
    assert not legal.act_allowed
    assert legal.legal_rows == ()


def test_no_legal_play_is_an_empty_mask_not_an_error() -> None:
    catalog = _catalog()
    deck = _deck()
    frame = _frame(catalog, deck)
    observation = _observation([deck[0]])

    blocked = legal_event_actions(
        _environment({}, play_allowed=False, hand_slots=(False, False, False, False)),
        observation,
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    assert not blocked.act_allowed and blocked.legal_rows == ()
    assert not blocked.playable_now

    empty_hand = legal_event_actions(
        _environment({"0": _placement(_grid([(1, 1)]))}),
        _observation([]),
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    assert not empty_hand.act_allowed

    slot_blocked = legal_event_actions(
        _environment({"0": _placement(_grid([(1, 1)]))}, hand_slots=(False, False, False, False)),
        observation,
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    assert not slot_blocked.act_allowed


def test_two_hand_copies_of_one_card_can_both_be_playable() -> None:
    """A duplicate deck entry appears as two rows; both must inherit the slot's
    legal cells instead of the sampler being able to pick one illegal row."""

    catalog = _catalog()
    duplicate = _deck()[3]
    deck = [duplicate] * 8
    frame = _frame(catalog, deck)
    environment = _environment({"0": _placement(_grid([(2, 2)]))})
    observation = _observation([duplicate, duplicate, 0, 0])

    legal = legal_event_actions(
        environment,
        observation,
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    assert legal.act_allowed
    assert legal.legal_rows == tuple(range(CARD_COUNT))
    assert int(legal.cell_masks[7].nonzero(as_tuple=False).flatten().tolist()[0]) == 2 * BOARD_WIDTH + 2


def test_decision_ready_defaults_true_when_the_engine_does_not_say() -> None:
    """A mask that carries no decision-window bit must not block decisions."""

    catalog = _catalog()
    deck = _deck()
    frame = _frame(catalog, deck)
    environment = _environment({"0": _placement(_grid([(3, 3)]))})
    legal = legal_event_actions(
        environment,
        _observation([deck[0]]),
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    assert legal.decision_ready is True


def test_decision_ready_is_carried_through() -> None:
    """The engine's window bit must survive: asking an actor whose play is still
    in flight records decisions the ring refuses, which is what made the live
    ACT rate look nothing like the pool's."""

    catalog = _catalog()
    deck = _deck()
    frame = _frame(catalog, deck)
    environment = _environment({"0": _placement(_grid([(3, 3)]))}, decision_ready=False)
    legal = legal_event_actions(
        environment,
        _observation([deck[0]]),
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
    )
    # Legality is unaffected; only the window is closed.
    assert legal.act_allowed is True
    assert legal.decision_ready is False


def test_minimum_playable_delay_reflects_elixir_accrual() -> None:
    """A short bar delays the play instead of forbidding it: the contract commits
    to a card and a delay, so the deliverable delay is the one that lands once
    elixir has accrued. 1 elixir per 2.8 s at 1x, doubled in double elixir."""

    # Affordable now: no wait.
    assert minimum_playable_delay_ms(elixir=5.0, cheapest_cost=4.0) == 0
    # Integer native accrual: 56*178=9968 raw is still short; use 57 ticks,
    # rounded to the next 100 ms bin. At 2x, 28*357=9996 is also short.
    assert minimum_playable_delay_ms(elixir=3.0, cheapest_cost=4.0) == 2900
    assert minimum_playable_delay_ms(elixir=3.0, cheapest_cost=4.0, elixir_multiplier=2.0) == 1500
    # Never negative, and it rounds up rather than down.
    assert minimum_playable_delay_ms(elixir=3.99, cheapest_cost=4.0) == 100

    bins = (0, 100, 200, 300)
    assert playable_delay_mask(bins, minimum_delay_ms=0) == (False, True, True, True)
    assert playable_delay_mask(bins, minimum_delay_ms=200) == (False, False, True, True)
    with pytest.raises(ValueError):
        minimum_playable_delay_ms(elixir=1.0, cheapest_cost=2.0, elixir_multiplier=0.0)


def test_unaffordable_card_becomes_a_deferred_deliverable_play() -> None:
    """Fix 1: a short elixir bar is not a forbidden card.

    The contract commits to a card *and a delay*, and the engine reports the
    slot's placements even while it closes the slot for insufficient_elixir — so
    the card stays selectable with a per-card delay floor that lands once the bar
    fills. Only when no delay in the vocabulary reaches that point is the card
    excluded.
    """

    catalog = _catalog()
    deck = _deck()
    frame = _frame(catalog, deck)
    # Slot 0 holds deck[0] costing 5.0 while the actor sits on 2.0.
    environment = _environment(
        {"0": _placement(_grid([(3, 3), (4, 4)]))},
        play_allowed=False,
        hand_slots=(False, False, False, False),
        costs=[5.0],
    )
    bins = (0, 100, 1000, 2000, 3000, 4000, 5000, 6000)
    legal = legal_event_actions(
        environment,
        _observation([deck[0]], elixir=2.0),
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
        delay_offset_ms=bins,
    )

    # 3 elixir short at 1x is 3 * 56 ticks = 8.4 s, past the 6 s vocabulary, so
    # no delay can deliver this card and it is excluded rather than offered as an
    # action the engine would refuse.
    assert not legal.playable_now
    assert not legal.act_allowed
    assert legal.legal_rows == ()

    # With a cheaper card (short by 1.0 = 2.8 s) the same slot stays deliverable.
    cheaper = _environment(
        {"0": _placement(_grid([(3, 3)]))},
        play_allowed=False,
        hand_slots=(False, False, False, False),
        costs=[3.0],
    )
    legal_cheap = legal_event_actions(
        cheaper,
        _observation([deck[0]], elixir=2.0),
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
        delay_offset_ms=bins,
    )
    assert legal_cheap.act_allowed
    mask = legal_cheap.delay_masks[0].tolist()
    assert mask == [offset >= 2800 for offset in bins]

    # Double elixir halves the wait.
    fast = legal_event_actions(
        cheaper,
        _observation([deck[0]], elixir=2.0, multiplier=2.0),
        frame,
        0,
        catalog=catalog,
        card_count=CARD_COUNT,
        cell_count=CELL_COUNT,
        board_width=BOARD_WIDTH,
        board_height=BOARD_HEIGHT,
        delay_offset_ms=bins,
    )
    assert fast.delay_masks[0].tolist() == [offset >= 1400 for offset in bins]
