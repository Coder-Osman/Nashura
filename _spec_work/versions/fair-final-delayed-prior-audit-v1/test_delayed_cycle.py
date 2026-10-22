from copy import deepcopy

import numpy as np
import pytest

from native_runner.training.fair_final.deck_belief import (
    DeckFrequency, FrozenDeckPrior, IncompatiblePublicCycle, PublicPlay,
)
from native_runner.training.fair_search.config import HOG_DECK

from delayed_cycle import DelayedCycleAudit


def cursor(opening_tick=90):
    prior = FrozenDeckPrior("fixed-experiment", "a" * 64,
        (DeckFrequency(tuple(sorted(HOG_DECK)), 1),))
    return DelayedCycleAudit(prior, owner=0, opening_tick=opening_tick)


def advance(belief, plays=(), tick=110):
    belief.advance(tuple(plays), from_tick=belief.tick, to_tick=tick,
        expected_evidence_sha=belief.evidence_sha)


@pytest.mark.parametrize("first,interval", [(100, 20), (2400, 20), (2401, 10), (4800, 10), (4801, 7)])
def test_refill_queue_and_phase_schedule(first, interval):
    belief = cursor(first - 2)
    cards = HOG_DECK[:2]
    advance(belief, [PublicPlay(first, cards[0]), PublicPlay(first + 1, cards[1])], first + 1)
    assert belief.predict()["holes"] == 1
    assert belief.next_refill == first + interval
    advance(belief, tick=first + interval)
    assert belief.predict()["holes"] == 0
    assert belief.predict()["hand_inclusion"][cards[1]] == 0


def test_incremental_rebuild_and_deepcopy_restore_are_exact():
    cards = HOG_DECK[:4]
    plays = [PublicPlay(tick, card) for tick, card in zip((100, 102, 104, 106), cards)]
    incremental, rebuilt = cursor(), cursor()
    for play in plays:
        advance(incremental, [play], play.tick)
    checkpoint = deepcopy(incremental)
    advance(incremental, tick=140)
    advance(rebuilt, plays, 140)
    advance(checkpoint, tick=140)
    assert incremental.support_digest() == rebuilt.support_digest() == checkpoint.support_digest()


def test_failed_update_keeps_support_and_clocks_unchanged():
    belief = cursor()
    before = belief.support_digest()
    card = HOG_DECK[0]
    with pytest.raises(IncompatiblePublicCycle):
        advance(belief, [PublicPlay(100, card), PublicPlay(101, card)], 101)
    assert before == belief.support_digest()
    with pytest.raises(ValueError):
        belief.advance((), from_tick=90, to_tick=100, expected_evidence_sha="b" * 64)
    assert before == belief.support_digest()


def test_scoring_hidden_labels_does_not_change_public_prediction():
    belief = cursor()
    advance(belief, [PublicPlay(100, HOG_DECK[0]), PublicPlay(102, HOG_DECK[1])], 102)
    before = belief.support_digest()
    prediction = belief.predict()
    current = belief._support[0][2]
    for candidate in current[:5]:
        hand = [int(card) for card in candidate[:4] if card != -1]
        result = belief.score(hand, int(candidate[4]))
        assert result["truth_in_support"]
    assert belief.support_digest() == before
    assert belief.predict() == prediction


def test_exact_hand_compatibility_rejects_wrong_hole_count():
    belief = cursor()
    advance(belief, [PublicPlay(100, HOG_DECK[0]), PublicPlay(102, HOG_DECK[1])], 102)
    candidate = belief._support[0][2][0]
    hand = [int(card) for card in candidate[:4] if card != -1]
    assert len(hand) == 3
    assert not belief.score(hand[:2], int(candidate[4]))["truth_in_support"]


def test_weighted_marginals_match_explicit_candidates():
    belief = cursor()
    entry = belief._support[0][0]
    orders = np.array([HOG_DECK, HOG_DECK[::-1]])
    belief._support = [(entry, orders, orders.copy()),
        (DeckFrequency(entry.cards, 3), orders[:1], orders[:1].copy())]
    prediction = belief.predict()
    assert prediction["next_card"][HOG_DECK[4]] == .8
    assert prediction["next_card"][HOG_DECK[3]] == .2
