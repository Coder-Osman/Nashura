from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_fair_search import frame, information, model, episode, SHA
from native_runner.training.event_ppo import _pad_dynamic_observation
from native_runner.training.event_checkpoint import _build_model
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.contracts import SearchDecision
from native_runner.training.fair_search.learner import SearchLearner, decision_batch_loss, decision_loss
from native_runner.training.fair_search.model import SearchPolicy
from native_runner.training.fair_search.replay import ReplayPool, resolve_replay_state


def clone_model(model):
    clone = SearchPolicy(_build_model(model.policy.backbone.config, model.policy.config,
                                     model.policy.event_config), value_head=model.value_head_architecture)
    clone.load_state_dict(model.state_dict())
    return clone.eval()


def varied_decisions(information):
    larger = _pad_dynamic_observation(
        information.frame, active_effect_count=information.frame.active_effects.mask.shape[1] + 2,
        relation_edge_count=information.frame.relation_edges.mask.shape[1] + 3,
        candidate_count=information.frame.candidates.mask.shape[1] + 1)
    wait_masks = {name: mask.clone() for name, mask in information.masks.items()}
    wait_masks["gate_mask"][0, 1] = False
    return [
        SearchDecision(0, information, {"wait": 32, "play:0:0:1": 0}, "wait", SHA, SHA, SHA, 1),
        SearchDecision(1, replace(information, owner=1, frame=larger),
                       {"wait": 3, "play:0:0:1": 21, "play:0:1:2": 8}, "play:0:1:2", SHA, SHA, SHA, -1),
        SearchDecision(2, replace(information, masks=wait_masks), {"wait": 32}, "wait", SHA, SHA, SHA, 0),
    ]


@pytest.mark.parametrize("detach", [False, True])
def test_batched_full_joint_loss_and_gradients_match_scalar(model, information, detach):
    torch.nn.init.normal_(model.wdl_head.weight, std=.03)
    reference = clone_model(model)
    decisions = varied_decisions(information)
    actual = decision_batch_loss(model, decisions, detach_value_features=detach)
    expected = tuple(torch.stack(values).mean() for values in zip(*(
        decision_loss(reference, row, detach_value_features=detach) for row in decisions)))
    for batch_value, scalar_value in zip(actual, expected):
        torch.testing.assert_close(batch_value, scalar_value, atol=2e-6, rtol=2e-5)
    actual[0].backward()
    expected[0].backward()
    for (name, parameter), (_, original) in zip(model.named_parameters(), reference.named_parameters()):
        assert (parameter.grad is None) == (original.grad is None), name
        if parameter.grad is not None:
            torch.testing.assert_close(parameter.grad, original.grad, atol=3e-6, rtol=3e-4, msg=name)


def test_uneven_microbatches_preserve_credit_sampling_and_update(model, information):
    config = SearchConfig(batch_size=5)
    batched = SearchLearner(model, ReplayPool(), config=config, microbatch_size=2)
    scalar = SearchLearner(clone_model(model), ReplayPool(), config=config, microbatch_size=1)
    for learner in (batched, scalar):
        learner.ingest(episode(information, count=3))
    batched_metrics = batched.train_available(max_steps=1)[0]
    scalar_metrics = scalar.train_available(max_steps=1)[0]
    assert batched.sample_credit == scalar.sample_credit == 1
    assert batched.replay.rng.getstate() == scalar.replay.rng.getstate()
    for key in ("policy_ce", "value_ce", "gradient_norm"):
        assert batched_metrics[key] == pytest.approx(scalar_metrics[key], abs=3e-6, rel=3e-4)
    for name, value in model.state_dict().items():
        torch.testing.assert_close(value, scalar.model.state_dict()[name], atol=2e-5, rtol=3e-4, msg=name)


def test_batched_illegal_mass_rejected(model, information):
    rows = varied_decisions(information)
    rows[0].visits.clear()
    rows[0].visits["play:7:0:1"] = 32
    with pytest.raises(ValueError, match="illegal"):
        decision_batch_loss(model, rows)


def test_external_replay_roundtrip_and_immutable_blob_reuse(tmp_path, information, monkeypatch):
    from native_runner.training.fair_search import replay as module

    replay = ReplayPool()
    replay.add(episode(information))
    state = replay.state_dict(directory=tmp_path)
    assert state["schema"] == "fair-search-replay.v2"
    reference = state["episodes"][0]
    path = tmp_path / reference["file"]
    before = path.stat().st_mtime_ns
    restored = ReplayPool()
    restored.load_state_dict(resolve_replay_state(state, tmp_path))
    assert restored.identities == replay.identities and restored.rows == replay.rows
    assert restored.rng.getstate() == replay.rng.getstate()

    def forbid_rewrite(*args, **kwargs):
        raise AssertionError("unchanged episode must not be reserialized")

    with monkeypatch.context() as context:
        context.setattr(module, "torch_bytes", forbid_rewrite)
        assert replay.state_dict(directory=tmp_path) == state
        assert restored.state_dict(directory=tmp_path) == state
    assert path.stat().st_mtime_ns == before
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        resolve_replay_state(state, tmp_path)


def test_external_replay_rejects_missing_blob_and_path_escape(tmp_path, information):
    replay = ReplayPool()
    replay.add(episode(information))
    state = replay.state_dict(directory=tmp_path)
    with pytest.raises(FileNotFoundError):
        resolve_replay_state(state, tmp_path / "missing")
    state["episodes"][0]["file"] = "../outside.pt.gz"
    with pytest.raises(ValueError, match="path"):
        resolve_replay_state(state, tmp_path)
