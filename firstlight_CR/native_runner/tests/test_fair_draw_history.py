from dataclasses import replace
import json

import pytest

from native_runner.tests.test_fair_search import episode, frame, information
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import DrawHistory, FrozenHistory, InformationState, semantic_digest


def test_rebinding_preserves_all_decisions_and_matches_full_replay():
    initial = HOG_DECK
    changed = (*initial[:2], initial[5], initial[3], initial[6], initial[2], initial[4], initial[7])
    original = DrawHistory(initial[:5])
    reference = DrawHistory(changed[:5])
    expected = [json.dumps(dict(initial_draw=list(changed[:5])), sort_keys=True)]
    for count in range(10):
        for decision in range(50):
            entry = dict(tick=count * 100 + decision, action="wait",
                         next_card=initial[4 + count] if count < 4 else initial[count % 8])
            original = original.append_decision(entry, count)
            entry["next_card"] = changed[4 + count] if count < 4 else initial[count % 8]
            reference = reference.append_decision(entry, count)
            expected.append(json.dumps(entry, sort_keys=True))
    rebound = original.rebind(changed)
    assert list(rebound) == list(reference) == expected
    assert semantic_digest(rebound) == semantic_digest(reference)
    assert all(before is after for before, after in zip(original.phases, rebound.phases))
    assert list(original) != expected


def test_hidden_unobserved_draws_do_not_enter_history_identity():
    initial = HOG_DECK
    history = DrawHistory(initial[:5]).append_decision(dict(tick=10, action="wait", next_card=initial[4]), 0)
    changed = (*initial[:5], *reversed(initial[5:]))
    assert semantic_digest(history.rebind(changed)) == semantic_digest(history)
    assert list(history.rebind(changed)) == list(history)


def test_history_hash_and_rebind_do_not_traverse_decisions(monkeypatch):
    history = DrawHistory(HOG_DECK[:5])
    for tick in range(10000):
        history = history.append_decision(dict(tick=tick, action="wait", next_card=HOG_DECK[4]), 0)

    def reject(*args):
        raise AssertionError("history traversal is forbidden")

    monkeypatch.setattr(FrozenHistory, "__iter__", reject)
    assert semantic_digest(history.rebind(HOG_DECK)) == semantic_digest(history)


def test_sampled_history_wire_roundtrip_retains_identity(information):
    history = DrawHistory(HOG_DECK[:5]).append_decision(dict(tick=0, action="wait", next_card=HOG_DECK[4]), 0)
    information = replace(information, own_history=history)
    restored = InformationState.from_payload(information.to_payload())
    assert restored.sha256 == information.sha256
    assert list(restored.own_history) == list(history)


def test_reject_changed_draw_without_execution():
    history = DrawHistory(HOG_DECK[:5]).append_decision(dict(tick=0, action="wait", next_card=HOG_DECK[4]), 0)
    with pytest.raises(ValueError, match="without a public play"):
        history.append_decision(dict(tick=1, action="wait", next_card=HOG_DECK[5]), 0)


def test_sampled_history_is_rejected_by_training(information):
    decision = episode(information).decisions[0]
    sampled = replace(information, own_history=DrawHistory(HOG_DECK[:5]))
    with pytest.raises(ValueError, match="not an actual training decision"):
        replace(decision, information=sampled)


def test_old_episode_schema_is_rejected(information):
    source = episode(information)
    with pytest.raises(ValueError):
        replace(source, schema="fair-search-episode.v1")
