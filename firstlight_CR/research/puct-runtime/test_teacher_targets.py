"""Training targets require all frozen replies and an actual completed match."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
import torch

from distillation import TeacherRecord, encode_model_inputs
from teacher_targets import finish_episode, reply_targets, terminal_evidence
import test_tree_search as fixtures
from tree_search import PuctSearch
from native_runner.tests.test_training_v4_model import _batch


def verified_record():
    frame, masks = encode_model_inputs(_batch(1), {
        "gate_mask": torch.ones(1, 2, dtype=torch.bool),
        "card_mask": torch.ones(1, 8, dtype=torch.bool),
        "cell_masks": torch.ones(1, 8, 576, dtype=torch.bool),
        "delay_mask": torch.ones(1, 8, 61, dtype=torch.bool),
    })
    evidence = dict(version="frozen-opponent-top-k.v1", root_action="hog", root_owner=0,
                    opponent_owner=1, top_k=4, temperature=.5, reply_legal_action_count=7,
                    reply_state_sha256="d" * 64, frozen_prior_sha256="e" * 64,
                    reply_probabilities={"a": .4, "b": .3, "c": .2, "d": .1},
                    reply_values={"a": -.8, "b": -.1, "c": .4, "d": .9})
    expected, robust, target = reply_targets(evidence)
    terminal = dict(version="native-terminal-evidence.v1", episode_id="game", ended=True,
                    truncated=False, winner=0, terminal_tick=6000, reason="native_final_result",
                    result_by_owner=[1., -1.], native_state_sha256="f" * 64)
    return replace(fixtures.DistillationTests.record("game", 0), semantic_frame=frame, action_masks=masks,
                   q_expected=expected, q_robust=robust, q_target=target,
                   value_evidence=evidence, terminal_evidence=terminal)


def test_verified_teacher_json_roundtrip_recomputes_top_four_and_result():
    record = verified_record()
    assert record.q_expected == pytest.approx(-.18)
    assert record.q_robust == -.8
    assert record.q_target == pytest.approx(-.366)
    encoded = json.loads(json.dumps(record.to_dict()))
    assert encoded["schema"] == "hog-tree-teacher-record.v3"
    TeacherRecord.from_dict(encoded).require_training_ready()


@pytest.mark.parametrize("damage", ["missing_reply", "wrong_q", "wrong_result", "truncated", "no_evidence"])
def test_training_rejects_incomplete_or_fabricated_evidence(damage):
    record = verified_record()
    if damage == "missing_reply":
        record.value_evidence["reply_values"].pop("d")
    elif damage == "wrong_q":
        record = replace(record, q_robust=record.q_expected, q_target=record.q_expected)
    elif damage == "wrong_result":
        record = replace(record, terminal_result=0.)
    elif damage == "truncated":
        record.terminal_evidence["truncated"] = True
    else:
        record = replace(record, value_evidence=None, terminal_evidence=None)
    with pytest.raises(ValueError):
        record.require_training_ready()


def test_real_search_details_cover_only_all_frozen_top_four():
    graph = {("root", "fire"): ("reply", .2)}
    graph.update({("reply", f"a{i}"): ("end", float(i - 3)) for i in range(6)})
    adapter = fixtures.FixtureAdapter(graph, {"root": 0, "reply": 1, "end": 0})
    search = PuctSearch(adapter, lambda _: {"fire": 1},
                        lambda _: {f"a{i}": 6 - i for i in range(6)}, lambda _: 0., gamma=1.)
    root = search.run(2)
    evidence = search.evaluate_root_reply_details(root, "fire")
    assert set(evidence["reply_values"]) == {"a0", "a1", "a2", "a3"}
    assert evidence["reply_values"] == pytest.approx({"a0": -2.8, "a1": -1.8, "a2": -.8, "a3": .2})
    assert evidence["reply_probabilities"]["a0"] == pytest.approx(36 / 86)
    assert adapter.current == "root"


def terminal_rollout():
    terminal = SimpleNamespace(ended=True, winner=1, result_by_owner=(-1., 1.),
                               reason="native_final_result", terminal_tick=6000)
    observation = SimpleNamespace(terminal=terminal, truncated=False)
    return SimpleNamespace(
        terminal=True, match_id="game", ready=[], tick=6000,
        environment=SimpleNamespace(observe_dual=lambda **_: {0: observation, 1: observation},
                                    native=SimpleNamespace(_request=lambda _: {"sha256": "f" * 64})),
        recorder=SimpleNamespace(open_rows=0, results=lambda: {0: -1., 1: 1.}),
        ledger=SimpleNamespace(counters=lambda: {"rejected": 0, "unconfirmed": 0}),
    )


def test_settlement_uses_native_winner_and_reward_ledger():
    result = terminal_evidence(terminal_rollout())
    assert result["winner"] == 1 and result["result_by_owner"] == [-1., 1.]
    rollout = terminal_rollout()
    rollout.recorder.results = lambda: {0: 0., 1: 0.}
    with pytest.raises(ValueError, match="ledger"):
        terminal_evidence(rollout)


def test_episode_timeout_does_not_create_a_draw_label():
    rollout = terminal_rollout()
    rollout.terminal = False
    with pytest.raises(TimeoutError, match="before native settlement"):
        finish_episode(rollout, max_seconds=0)
    with pytest.raises(ValueError, match="settlement"):
        terminal_evidence(rollout)
