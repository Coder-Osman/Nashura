"""Persistent boundaries preserve PPO math and reject broken checkpoint chains."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_optimizer_resume import _collect, _assert_tree_equal
from native_runner.training import event_checkpoint
from native_runner.training.event_inference import InferenceModel
from tools import event_rl_update as cli
from tools.event_rl_learner import CheckpointVerifier, Learner


@pytest.fixture
def initial(tmp_path, monkeypatch):
    def build(backbone=None, *_):
        model = _event_model()
        model.backbone.config = backbone or replace(model.backbone.config, snapshot_only=True)
        return model
    monkeypatch.setattr(event_checkpoint, "_build_model", build)
    monkeypatch.setattr("native_runner.training.event_inference._build_model", build)
    torch.manual_seed(91)
    checkpoint = tmp_path / "initial.pt"
    event_checkpoint.save_event_checkpoint(checkpoint, build())
    return checkpoint


def config(initial, tmp_path):
    return dict(argv=["--checkpoint", str(initial), "--cache-root", str(tmp_path),
                      "--output", str(tmp_path / "unused.pt"), "--minibatch-lanes", "0",
                      "--expected-temperatures", "1,1,1,1", "--lr", "0.00001"],
                inference_directory=str(tmp_path / "inference"))


def task_for(worker, checkpoint, tmp_path, iteration):
    cache = tmp_path / f"cache-{iteration}"
    cache.mkdir()
    loaded = worker.session.loaded
    _collect(loaded.model, cache / "part-000.crev.zst", loaded.checkpoint_sha256, loaded.contract_sha256)
    return dict(request_id=str(iteration), iteration=iteration,
                parent_checkpoint_sha256=loaded.checkpoint_sha256, checkpoint=str(checkpoint),
                cache_root=str(cache), output=str(tmp_path / f"child-{iteration}.pt"),
                log=str(tmp_path / f"update-{iteration}.log"))


def test_two_hot_rounds_equal_two_cold_updates_and_export_current_child(initial, tmp_path):
    worker = Learner(config(initial, tmp_path))
    verifier = CheckpointVerifier(initial)
    checkpoint = initial
    optimizer_id = None
    model_id = id(worker.session.loaded.model)
    for iteration in (1, 2):
        task = task_for(worker, checkpoint, tmp_path, iteration)
        args = cli.parse_args(config(checkpoint, tmp_path)["argv"])
        args.cache_root = Path(task["cache_root"])
        args.output = tmp_path / f"cold-{iteration}.pt"
        assert cli.UpdateSession(args).update(args) == 0
        result = worker.update(task)
        verified = verifier.verify({**task, **result})
        assert verified["iterations"] == verified["optimizer_steps"] == iteration
        assert verified["decision_rows"] == 3 * iteration
        assert result["descriptor"]["source_checkpoint_sha256"] == verified["checkpoint_sha256"]
        hot = event_checkpoint.load_event_checkpoint(task["output"])
        cold = event_checkpoint.load_event_checkpoint(args.output)
        _assert_tree_equal(hot.model.state_dict(), cold.model.state_dict())
        _assert_tree_equal(hot.payload[cli.PPO_TRAINING_KEY], cold.payload[cli.PPO_TRAINING_KEY])
        inference, _ = InferenceModel().load(result["descriptor"])
        _assert_tree_equal(inference.state_dict(), hot.model.state_dict())
        assert id(worker.session.loaded.model) == model_id
        if optimizer_id is not None:
            assert id(worker.session.optimizer) == optimizer_id
        optimizer_id = id(worker.session.optimizer)
        checkpoint = Path(task["output"])


@pytest.mark.parametrize("problem", ["parent", "iteration", "output"])
def test_invalid_request_rejected_before_update(initial, tmp_path, monkeypatch, problem):
    worker = Learner(config(initial, tmp_path))
    task = task_for(worker, initial, tmp_path, 1)
    if problem == "parent":
        task["parent_checkpoint_sha256"] = "0" * 64
    elif problem == "iteration":
        task["iteration"] = 0
    else:
        Path(task["output"]).write_text("keep")
    monkeypatch.setattr(worker.session, "update", lambda _: pytest.fail("invalid request reached PPO"))
    with pytest.raises(ValueError):
        worker.update(task)
    assert not Path(task["log"]).exists()


def test_failed_preflight_poison_session_without_advancing(initial, tmp_path):
    worker = Learner(config(initial, tmp_path))
    task = task_for(worker, initial, tmp_path, 1)
    parent_sha = worker.session.loaded.checkpoint_sha256
    with torch.no_grad():
        bias = worker.session.loaded.model.delay_head[-1].bias
        bias.add_(torch.arange(bias.numel()) * 3)
    with pytest.raises(SystemExit, match="preflight failed"):
        worker.update(task)
    assert worker.session.failed
    assert worker.session.optimizer is None
    assert worker.session.loaded.checkpoint_sha256 == parent_sha
    assert not Path(task["output"]).exists()
    with pytest.raises(RuntimeError, match="session failed"):
        worker.session.update(worker.args)


@pytest.mark.parametrize("problem", ["parent", "hash", "counters", "contract", "nonfinite_optimizer", "corrupt"])
def test_independent_readback_rejects_damage(initial, tmp_path, problem):
    worker = Learner(config(initial, tmp_path))
    verifier = CheckpointVerifier(initial)
    before = dict(verifier.current)
    task = task_for(worker, initial, tmp_path, 1)
    result = worker.update(task)
    request = {**task, **result}
    if problem == "parent":
        request["parent_checkpoint_sha256"] = "0" * 64
    elif problem == "hash":
        request["checkpoint_sha256"] = "0" * 64
    elif problem == "counters":
        request["optimizer_steps"] += 1
    elif problem == "corrupt":
        Path(task["output"]).write_bytes(b"corrupt")
    else:
        payload = torch.load(task["output"], weights_only=True)
        if problem == "contract":
            payload["contract_sha256"] = "0" * 64
        else:
            state = payload[cli.PPO_TRAINING_KEY]["optimizer"]["state"]
            next(iter(state.values()))["exp_avg"].fill_(float("nan"))
        torch.save(payload, task["output"])
    with pytest.raises(Exception):
        verifier.verify(request)
    assert verifier.current == before
    assert verifier.failed


def test_options_fixed_across_rounds(initial, tmp_path):
    args = cli.parse_args(config(initial, tmp_path)["argv"])
    session = cli.UpdateSession(args)
    args.lr = 0.1
    with pytest.raises(ValueError, match="options changed"):
        session.update(args)
