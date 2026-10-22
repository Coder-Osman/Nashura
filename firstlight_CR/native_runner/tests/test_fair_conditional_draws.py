from copy import deepcopy
from dataclasses import replace
import json
import random
from types import SimpleNamespace

import numpy as np
import pytest

from native_runner.tests.test_fair_search import frame, information
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import PublicEvent
from native_runner.training.fair_search.draw import ConditionalDraws


CATALOG = SimpleNamespace(vocab_id=lambda card: HOG_DECK.index(card))


def current_information(information, queues, events, decisions):
    semantic = deepcopy(information.frame)
    own = semantic.own_cards
    own.card_vocab_id[0] = own.card_vocab_id.new_tensor(range(8))
    own.mask[:] = True
    own.runtime_features[:, :, :2] = 0
    for card in queues[information.owner][:4]:
        if card != -1:
            own.runtime_features[0, HOG_DECK.index(card), 0] = 1
    own.runtime_features[0, HOG_DECK.index(queues[information.owner][4]), 1] = 1
    initial = json.dumps(dict(initial_draw=list(HOG_DECK[:5])))
    return replace(information, tick=max((event.tick for event in events), default=100), frame=semantic,
                   public_history=tuple(events), own_history=(initial, *decisions))


@pytest.mark.parametrize("owner", [0, 1])
def test_incremental_conditioning_equals_complete_history_reference(information, owner):
    information = replace(information, owner=owner)
    queues = {0: list(HOG_DECK), 1: list(HOG_DECK)}
    queues[1 - owner] = list(reversed(HOG_DECK))
    pool = ConditionalDraws(owner, HOG_DECK[:5])
    events, decisions = [], []
    for index in range(24):
        side = index % 2
        if side == owner:
            decisions.append(json.dumps(dict(tick=100 + len(events) * 30, action="wait", next_card=queues[side][4])))
        card = queues[side][index % 4]
        hand_index = queues[side].index(card)
        queues[side][hand_index] = queues[side][4]
        queues[side][4:7] = queues[side][5:8]
        queues[side][7] = card
        events.append(PublicEvent(side, 130 + index * 30, card, 500, 500))
        current = current_information(information, queues, events, decisions)
        before = {side: values.copy() for side, values in pool.current.items()}
        updated, previous_queues = pool.propose(current, CATALOG, rng=random.Random(index))
        reference, _ = ConditionalDraws(owner, HOG_DECK[:5]).propose(current, CATALOG, rng=random.Random(index))
        for side in (0, 1):
            np.testing.assert_array_equal(updated.initial[side], reference.initial[side])
            np.testing.assert_array_equal(updated.current[side], reference.current[side])
            np.testing.assert_array_equal(pool.current[side], before[side])
            assert any(tuple(row) == previous_queues[side] for row in before[side])
        pool = updated
    assert pool.public_cursor == 24


def test_conditioning_rejects_impossible_play_without_mutating_pool(information):
    pool = ConditionalDraws(0, HOG_DECK[:5])
    queues = {0: list(HOG_DECK), 1: list(HOG_DECK)}
    current = current_information(information, queues, [PublicEvent(0, 101, HOG_DECK[7], 500, 500)], [])
    before = {side: values.copy() for side, values in pool.current.items()}
    with pytest.raises(ValueError, match="no draw permutation"):
        pool.propose(current, CATALOG, rng=random.Random(1))
    for side in (0, 1):
        np.testing.assert_array_equal(pool.current[side], before[side])
    assert pool.public_cursor == 0


def test_cancelled_wait_does_not_advance_cycle_and_duplicate_root_is_stable(information):
    pool = ConditionalDraws(0, HOG_DECK[:5])
    queues = {0: list(HOG_DECK), 1: list(HOG_DECK)}
    decisions = [json.dumps(dict(tick=100, action="play:0:0:1", next_card=HOG_DECK[4]))]
    current = current_information(information, queues, [], decisions)
    updated, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    duplicate, _ = updated.propose(current, CATALOG, rng=random.Random(2))
    for side in (0, 1):
        np.testing.assert_array_equal(duplicate.current[side], updated.current[side])
        np.testing.assert_array_equal(updated.current[side], pool.current[side])


def test_delayed_refill_matches_native_twenty_tick_cadence(information):
    pool = ConditionalDraws(0, HOG_DECK[:5])
    queues = {0: list(HOG_DECK), 1: list(HOG_DECK)}
    events = [PublicEvent(0, 402, HOG_DECK[0], 500, 500)]
    queues[0] = [HOG_DECK[4], *HOG_DECK[1:4], *HOG_DECK[5:], HOG_DECK[0]]
    current = replace(current_information(information, queues, events, []), tick=402)
    pool, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    events.append(PublicEvent(0, 405, HOG_DECK[4], 500, 500))
    queues[0] = [-1, *HOG_DECK[1:4], *HOG_DECK[5:], HOG_DECK[0], HOG_DECK[4]]
    current = replace(current_information(information, queues, events, []), tick=405)
    pool, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    assert pool.current[0].shape[1] == 9
    assert pool.next_refill[0] == 422
    current = replace(current, tick=421)
    pool, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    assert pool.current[0].shape[1] == 9
    queues[0] = [HOG_DECK[5], *HOG_DECK[1:4], *HOG_DECK[6:], HOG_DECK[0], HOG_DECK[4]]
    current = replace(current_information(information, queues, events, []), tick=422)
    pool, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    assert pool.current[0].shape[1] == 8
    assert pool.next_refill[0] == 442
    np.testing.assert_array_equal(pool.current[0][0], queues[0])


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("first_tick,refill_tick,next_tick", [
    (2399, 2419, 2429), (2400, 2420, 2430), (2401, 2411, 2421),
    (3257, 3267, 3277), (4799, 4809, 4816), (4800, 4810, 4817),
    (4801, 4808, 4815), (5982, 5989, 5996),
])
def test_refill_deadline_uses_phase_when_card_was_drawn(information, owner, first_tick, refill_tick, next_tick):
    information = replace(information, owner=owner)
    pool = ConditionalDraws(owner, HOG_DECK[:5])
    queues = {0: list(HOG_DECK), 1: list(HOG_DECK)}
    events = [PublicEvent(owner, first_tick, HOG_DECK[0], 500, 500)]
    queues[owner] = [HOG_DECK[4], *HOG_DECK[1:4], *HOG_DECK[5:], HOG_DECK[0]]
    current = current_information(information, queues, events, [])
    pool, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    assert pool.next_refill[owner] == refill_tick
    events.append(PublicEvent(owner, first_tick + 6, HOG_DECK[4], 500, 500))
    queues[owner] = [-1, *HOG_DECK[1:4], *HOG_DECK[5:], HOG_DECK[0], HOG_DECK[4]]
    current = replace(current_information(information, queues, events, []), tick=refill_tick - 1)
    pool, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    queues[owner] = [HOG_DECK[5], *HOG_DECK[1:4], *HOG_DECK[6:], HOG_DECK[0], HOG_DECK[4]]
    current = replace(current_information(information, queues, events, []), tick=refill_tick)
    updated, _ = pool.propose(current, CATALOG, rng=random.Random(1))
    reference, _ = ConditionalDraws(owner, HOG_DECK[:5]).propose(current, CATALOG, rng=random.Random(1))
    assert updated.next_refill[owner] == next_tick
    assert pool.next_refill[owner] == refill_tick
    np.testing.assert_array_equal(updated.current[owner], reference.current[owner])
    np.testing.assert_array_equal(updated.current[owner][0], queues[owner])
