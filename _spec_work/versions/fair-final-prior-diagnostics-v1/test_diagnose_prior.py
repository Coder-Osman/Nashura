from types import SimpleNamespace

import numpy as np
import pytest
import torch

import diagnose_prior


def cursor():
    return SimpleNamespace(_support=[
        (SimpleNamespace(cards=tuple(range(8)), count=1), None,
            np.asarray([[0, 1, 2, 3, 4, 5, 6, 7]])),
        (SimpleNamespace(cards=tuple(range(8)), count=3), None,
            np.asarray([[0, 1, 2, 5, 6, 3, 4, 7]]))])


@pytest.mark.parametrize("hand", [[], [0], [0, 1], [0, 1, 2], [0, 1, 2, 3]])
def test_partial_hand_compatibility_uses_weighted_joint_mass(hand):
    belief = cursor()
    result = diagnose_prior.score(diagnose_prior.predict(belief), hand, 4, belief)
    assert result["joint_compatible_probability_mass"] == .25
    assert result["truth_in_support"] is True
    assert result["next_nll"] == pytest.approx(-np.log(.25))
    assert result["next_brier"] == pytest.approx(1.125)
    assert "hand_brier" not in result


def test_compatibility_excludes_missing_hand_card_and_mismatched_next():
    belief = cursor()
    prediction = diagnose_prior.predict(belief)
    result = diagnose_prior.score(prediction, [5], 4, belief)
    assert result["truth_in_support"] is False
    assert result["joint_compatible_probability_mass"] == 0
    result = diagnose_prior.score(prediction, [], 7, belief)
    assert result["zero_next_probability"] is True
    assert result["next_nll"] is None


@pytest.mark.parametrize("count", range(5))
def test_truth_accepts_native_refill_gap(monkeypatch, count):
    cards = list(diagnose_prior.HOG_DECK)
    own = SimpleNamespace(mask=torch.ones(1, 8, dtype=torch.bool),
        card_vocab_id=torch.arange(8).reshape(1, 8), runtime_features=torch.zeros(1, 8, 2))
    own.runtime_features[0, :count, 0] = 1
    own.runtime_features[0, 7, 1] = 1
    monkeypatch.setattr(diagnose_prior, "information_from_row",
        lambda row: SimpleNamespace(frame=SimpleNamespace(own_cards=own)))
    hand, upcoming = diagnose_prior.truth_from_row({}, SimpleNamespace(vocab_id=cards.index))
    assert hand == sorted(cards[:count])
    assert upcoming == cards[7]


@pytest.mark.parametrize("bad", ["five_cards", "duplicate", "missing_next", "two_next", "next_in_hand", "nonbinary"])
def test_invalid_truth_is_rejected(monkeypatch, bad):
    cards = list(diagnose_prior.HOG_DECK)
    own = SimpleNamespace(mask=torch.ones(1, 8, dtype=torch.bool),
        card_vocab_id=torch.arange(8).reshape(1, 8), runtime_features=torch.zeros(1, 8, 2))
    own.runtime_features[0, :2, 0] = 1
    own.runtime_features[0, 7, 1] = 1
    if bad == "five_cards":
        own.runtime_features[0, :5, 0] = 1
    elif bad == "duplicate":
        own.card_vocab_id[0, 1] = 0
    elif bad == "missing_next":
        own.runtime_features[0, 7, 1] = 0
    elif bad == "two_next":
        own.runtime_features[0, 6, 1] = 1
    elif bad == "next_in_hand":
        own.runtime_features[0, 7, 1] = 0
        own.runtime_features[0, 0, 1] = 1
    else:
        own.runtime_features[0, 0, 0] = .5
    monkeypatch.setattr(diagnose_prior, "information_from_row",
        lambda row: SimpleNamespace(frame=SimpleNamespace(own_cards=own)))
    with pytest.raises(ValueError):
        diagnose_prior.truth_from_row({}, SimpleNamespace(vocab_id=cards.index))


def test_empty_group_is_reported_without_invented_metrics():
    result = diagnose_prior.summarize([])
    assert result["rows"] == result["games"] == 0
    assert result["equal_game_mean"] is None


def test_summary_weights_games_equally_and_calibrates_next_card():
    belief = cursor()
    prediction = diagnose_prior.predict(belief)
    rows = [dict(game=game, prediction=prediction, truth_next=upcoming,
        score=diagnose_prior.score(prediction, [], upcoming, belief))
        for game, upcoming in (("short", 4), ("long", 6), ("long", 6), ("long", 6))]
    result = diagnose_prior.summarize(rows)
    assert result["equal_game_mean"]["joint_compatible_probability_mass"] == .5
    assert result["equal_game_mean"]["next_brier"] == pytest.approx(.625)
    assert result["next_reliability_frame_weighted_descriptive"]
    assert "hand_reliability_frame_weighted_descriptive" not in result
