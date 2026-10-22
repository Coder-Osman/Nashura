from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
import torch

from native_runner.training import event_checkpoint as checkpoint
from native_runner.training.event_inference import InferenceModel, export_inference
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4
from tools.event_rl_continue import resume_point


@pytest.fixture(scope="module")
def model():
    return checkpoint._build_model(
        replace(scaled_model_config_v4(0.125), snapshot_only=True),
        EventModelConfigV1(delay_hidden=32, cell_hidden=16, residual_blocks=2, residual_hidden=32),
        EventPolicyConfigV1())


def test_pure_artifact_preserves_parameters_and_hot_loads(tmp_path, model):
    path = tmp_path / "full.pt"
    original_sha = checkpoint.save_event_checkpoint(path, model, extra={
        "event_ppo_training": {"optimizer": {"state": {1: torch.ones(100)}}}, "policy_version": "original-policy"})
    descriptor = export_inference(path, tmp_path / "artifacts")
    raw = torch.load(descriptor["path"], weights_only=True)
    assert "event_ppo_training" not in raw
    assert raw["source_checkpoint_sha256"] == original_sha
    worker = InferenceModel()
    loaded, policy = worker.load(descriptor)
    assert policy == "original-policy"
    for name, tensor in model.state_dict().items():
        torch.testing.assert_close(loaded.state_dict()[name], tensor, rtol=0, atol=0)
    # Simulate a later trained checkpoint, without changing the original fixture.
    updated = checkpoint.event_checkpoint_payload(model)
    updated["model"] = {k: v.clone() for k, v in updated["model"].items()}
    key = next(k for k,v in updated["model"].items() if v.is_floating_point())
    updated["model"][key].add_(0.25)
    torch.save(updated, path)
    next_descriptor = export_inference(path, tmp_path / "artifacts")
    reloaded, _ = worker.load(next_descriptor)
    assert reloaded is loaded
    torch.testing.assert_close(reloaded.state_dict()[key], updated["model"][key], rtol=0, atol=0)
    assert next_descriptor["source_checkpoint_sha256"] != original_sha


@pytest.mark.parametrize("field", ["artifact_sha256", "source_checkpoint_sha256", "contract_sha256"])
def test_wrong_task_identity_rejected(tmp_path, model, field):
    path = tmp_path / "full.pt"
    checkpoint.save_event_checkpoint(path, model)
    descriptor = export_inference(path, tmp_path / "artifacts")
    with pytest.raises(ValueError, match="SHA mismatch"):
        InferenceModel().load({**descriptor, field: "0" * 64})


def test_resume_rejects_gaps_and_incomplete_rounds(tmp_path):
    root = tmp_path
    (root / "weights").mkdir()
    content = b"test-complete-checkpoint"
    sha = hashlib.sha256(content).hexdigest()
    path = root / "weights" / (sha + ".pt")
    path.write_bytes(content)
    config = {"start_iteration": 171, "initial_checkpoint_sha256": sha}
    assert resume_point(root, config) == (171, path)
    directory = root / "iteration-000172"
    directory.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        resume_point(root, config)
    (directory / "success.json").write_text(json.dumps({"iteration": 172, "checkpoint": str(path)}))
    assert resume_point(root, config) == (172, path)
    later = root / "iteration-000174"
    later.mkdir()
    (later / "success.json").write_text(json.dumps({"iteration": 174, "checkpoint": str(path)}))
    with pytest.raises(ValueError, match="contiguous"):
        resume_point(root, config)
