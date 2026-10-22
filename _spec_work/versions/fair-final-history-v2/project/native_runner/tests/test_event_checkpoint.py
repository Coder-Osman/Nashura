"""Shared checkpoint semantics and metadata validation."""
from dataclasses import asdict, replace
import hashlib

import pytest
import torch

from native_runner.training import event_checkpoint as checkpoint
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4


@pytest.fixture
def model():
    return checkpoint._build_model(
        replace(scaled_model_config_v4(0.125), snapshot_only=False),
        EventModelConfigV1(delay_hidden=32, cell_hidden=16),
        EventPolicyConfigV1(max_wait_ticks=123),
    )


def test_full_roundtrip_ignores_environment(tmp_path, monkeypatch, model):
    monkeypatch.setenv("CR_EVENT_MODEL_SCALE", "9")
    monkeypatch.setenv("CR_EVENT_MODEL_SNAPSHOT_ONLY", "1")
    path = tmp_path / "event.pt"
    identity = checkpoint.save_event_checkpoint(path, model, extra={"step": 7})
    loaded = checkpoint.load_event_checkpoint(path)
    assert identity == loaded.checkpoint_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert loaded.model.backbone.config == model.backbone.config
    assert loaded.model.config == model.config
    assert loaded.model.event_config == model.event_config
    assert loaded.model.backbone.config.snapshot_only is False
    assert loaded.payload["step"] == 7
    assert loaded.contract_sha256 == loaded.payload["contract_sha256"]
    for name, tensor in model.state_dict().items():
        torch.testing.assert_close(loaded.model.state_dict()[name], tensor)


@pytest.mark.parametrize("raw, expected", [(" FALSE ", False), (" 0 ", False), ("TrUe", True), ("1", True), (False, False)])
def test_legacy_semantics_and_scale(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("CR_EVENT_MODEL_SCALE", "2")
    monkeypatch.setenv("CR_EVENT_MODEL_SNAPSHOT_ONLY", str(int(not expected)))
    backbone = replace(scaled_model_config_v4(0.125), snapshot_only=expected)
    model = checkpoint._build_model(backbone, EventModelConfigV1(), EventPolicyConfigV1())
    path = tmp_path / "legacy.pt"
    torch.save({"model": model.state_dict(), "snapshot_only": raw, "model_scale": " 0.125 "}, path)
    loaded = checkpoint.load_event_checkpoint(path, initialization_only=True)
    assert loaded.model.backbone.config == backbone


@pytest.mark.parametrize("metadata, match", [
    ({"model_scale": "1"}, "missing snapshot_only"),
    ({"snapshot_only": "0"}, "missing model_scale"),
    ({"snapshot_only": "", "model_scale": "1"}, "explicit boolean"),
    ({"snapshot_only": "maybe", "model_scale": "1"}, "explicit boolean"),
    ({"snapshot_only": "0", "model_scale": "nan"}, "finite positive"),
    ({"snapshot_only": "0", "model_scale": "0"}, "finite positive"),
    ({"snapshot_only": "0", "model_scale": True}, "finite positive"),
])
def test_invalid_legacy_metadata(tmp_path, metadata, match):
    path = tmp_path / "bad.pt"
    torch.save({"model": {}, **metadata}, path)
    with pytest.raises(ValueError, match=match):
        checkpoint.load_event_checkpoint(path, initialization_only=True)


def test_explicit_legacy_override_and_padding(tmp_path):
    backbone = replace(scaled_model_config_v4(0.125), snapshot_only=True)
    model = checkpoint._build_model(backbone, EventModelConfigV1(), EventPolicyConfigV1())
    state = model.state_dict()
    width = state["delay_head.0.weight"].shape[1] - model.config.cell_hidden
    state["delay_head.0.weight"] = state["delay_head.0.weight"][:, :width].clone()
    path = tmp_path / "legacy.pt"
    torch.save({"model": state}, path)
    loaded = checkpoint.load_event_checkpoint(path, initialization_only=True, legacy_snapshot_only=True, legacy_model_scale=0.125)
    assert loaded.model.backbone.config == backbone
    assert torch.count_nonzero(loaded.model.state_dict()["delay_head.0.weight"][:, width:]) == 0


@pytest.mark.parametrize("mutation, match", [
    (lambda c: c.update(version="future"), "unsupported"),
    (lambda c: c["backbone_config"].pop("snapshot_only"), "missing"),
    (lambda c: c["backbone_config"].update(snapshot_only="false"), "invalid type"),
    (lambda c: c["model_config"].update(delay_hidden=0), "positive"),
    (lambda c: c["event_config"].update(unknown=1), "unknown"),
])
def test_invalid_new_metadata(tmp_path, model, mutation, match):
    payload = checkpoint.event_checkpoint_payload(model)
    mutation(payload[checkpoint.CONTRACT_KEY])
    path = tmp_path / "bad.pt"
    torch.save(payload, path)
    with pytest.raises(ValueError, match=match):
        checkpoint.load_event_checkpoint(path)


def test_json_config_lists_and_identity(tmp_path, model):
    import json
    payload = checkpoint.event_checkpoint_payload(model)
    payload[checkpoint.CONTRACT_KEY] = json.loads(json.dumps(payload[checkpoint.CONTRACT_KEY]))
    path = tmp_path / "json.pt"
    torch.save(payload, path)
    loaded = checkpoint.load_event_checkpoint(path)
    assert asdict(loaded.model.event_config) == asdict(model.event_config)
    payload["contract_sha256"] = "0" * 64
    torch.save(payload, path)
    with pytest.raises(ValueError, match="contract_sha256"):
        checkpoint.load_event_checkpoint(path)


def test_state_shape_mismatch_fails(tmp_path, model):
    payload = checkpoint.event_checkpoint_payload(model)
    payload["model"]["gate_head.0.weight"] = torch.zeros(1, 1)
    path = tmp_path / "mismatch.pt"
    torch.save(payload, path)
    with pytest.raises(ValueError, match="weights do not match"):
        checkpoint.load_event_checkpoint(path)


def test_conflicting_semantics_fail(tmp_path, model):
    payload = checkpoint.event_checkpoint_payload(model)
    payload["snapshot_only"] = "true"
    path = tmp_path / "conflict.pt"
    torch.save(payload, path)
    with pytest.raises(ValueError, match="conflicts"):
        checkpoint.load_event_checkpoint(path)
    with pytest.raises(ValueError, match="overrides"):
        checkpoint.load_event_checkpoint(path, legacy_snapshot_only=False)


def test_legacy_partial_configs(tmp_path, model):
    path = tmp_path / "legacy-config.pt"
    payload = {
        "model": model.state_dict(), "snapshot_only": False, "model_scale": 0.125,
        "model_config": {"delay_hidden": 32, "cell_hidden": 16},
        "event_config": {"max_wait_ticks": 123, "delay_offset_ms": list(model.event_config.delay_offset_ms)},
    }
    torch.save(payload, path)
    loaded = checkpoint.load_event_checkpoint(path, initialization_only=True)
    assert loaded.model.config == model.config
    assert loaded.model.event_config == model.event_config
    payload["event_config"]["delay_offset_ms"] = [0, 100]
    torch.save(payload, path)
    with pytest.raises(ValueError, match="delay vocabulary"):
        checkpoint.load_event_checkpoint(path, initialization_only=True)
    payload["event_config"] = {"unknown": 1}
    torch.save(payload, path)
    with pytest.raises(ValueError, match="unknown"):
        checkpoint.load_event_checkpoint(path, initialization_only=True)


def test_atomic_save_creates_parents_and_preserves_old_file(tmp_path, monkeypatch, model):
    path = tmp_path / "nested" / "event.pt"
    checkpoint.save_event_checkpoint(path, model)
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(checkpoint.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        checkpoint.save_event_checkpoint(path, model, extra={"step": 2})
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


def test_reserved_extra_rejected(model):
    with pytest.raises(ValueError, match="reserved"):
        checkpoint.event_checkpoint_payload(model, extra={"snapshot_only": "1"})


@pytest.mark.parametrize("versioned", [False, True])
def test_explicit_initialization_lifecycle(tmp_path, model, versioned):
    payload = checkpoint.event_checkpoint_payload(model)
    if versioned:
        contract = payload[checkpoint.CONTRACT_KEY]
        contract.update(version="event-checkpoint.v1", model_version="event-card-policy-model.v1")
        contract.pop("input_semantics")
        contract.pop("action_semantics")
        payload["contract_sha256"] = checkpoint._contract_hash(contract)
    else:
        payload.pop(checkpoint.CONTRACT_KEY)
        payload.pop("contract_sha256")
        payload.update(snapshot_only=False, model_scale=0.125,
                       model_config=asdict(model.config), event_config=asdict(model.event_config))
    payload.update(rank=1, evaluation={"win_rate": 1.0}, optimizer={"stale": True})
    path = tmp_path / "old.pt"
    torch.save(payload, path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="initialization_only"):
        checkpoint.load_event_checkpoint(path)
    with pytest.warns(UserWarning, match="change expected behavior"):
        loaded = checkpoint.load_event_checkpoint(path, initialization_only=True)
    assert path.read_bytes() == original
    assert not {"rank", "evaluation", "optimizer"}.intersection(loaded.payload)
    marker = loaded.payload[checkpoint.INITIALIZATION_KEY]
    assert marker["migrated_from"]["checkpoint_sha256"] == hashlib.sha256(original).hexdigest()
    assert marker["needs_recalibration_or_finetune"] is True
    for name, tensor in model.state_dict().items():
        torch.testing.assert_close(loaded.model.state_dict()[name], tensor)
    pending = tmp_path / "pending.pt"
    checkpoint.save_event_checkpoint(pending, loaded.model, extra={"step": 100})
    with pytest.raises(ValueError, match="still needs"):
        checkpoint.load_event_checkpoint(pending)
    with pytest.warns(UserWarning):
        resumed = checkpoint.load_event_checkpoint(pending, initialization_only=True)
    checkpoint.complete_event_initialization(resumed.model)
    checkpoint.save_event_checkpoint(pending, resumed.model)
    trained = checkpoint.load_event_checkpoint(pending)
    trained_marker = trained.payload[checkpoint.INITIALIZATION_KEY]
    assert trained_marker["needs_recalibration_or_finetune"] is False
    assert trained_marker["needs_evaluation"] is True
    assert trained_marker["migrated_from"] == marker["migrated_from"]


@pytest.mark.parametrize("field", ["input_semantics", "action_semantics", "model_version"])
def test_incompatible_semantics_rejected_even_for_initialization(tmp_path, model, field):
    payload = checkpoint.event_checkpoint_payload(model)
    payload[checkpoint.CONTRACT_KEY][field] = "unknown"
    path = tmp_path / "bad-semantics.pt"
    torch.save(payload, path)
    for initialization_only in (False, True):
        with pytest.raises(ValueError, match="unsupported"):
            checkpoint.load_event_checkpoint(path, initialization_only=initialization_only)
