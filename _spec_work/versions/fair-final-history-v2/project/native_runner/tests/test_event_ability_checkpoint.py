"""Skill architecture identity and exact preservation of existing PPO state."""
from dataclasses import replace
import json
import sys

import pytest
import torch

from native_runner.training import event_checkpoint as cp
from native_runner.training.event_ability_migration import migrate_ability_checkpoint
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4
from tools.event_rl_update import optimizer_checkpoint, restore_optimizer, UpdateSession, parse_args
from types import SimpleNamespace


@pytest.mark.parametrize("residual", [False, True])
def test_migration_preserves_parameters_moments_controller_and_strict_contract(tmp_path, residual):
    source = cp._build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
        EventModelConfigV1(residual_blocks=int(residual), residual_hidden=32), EventPolicyConfigV1())
    optimizer, progress = restore_optimizer(source, {})
    # A nontrivial actual AdamW step makes dropped/misassigned moments observable.
    selected = [source.gate_head[0].weight, source.card_query.weight]
    for parameter in selected:
        parameter.grad = torch.ones_like(parameter)
    optimizer.step()
    saved = optimizer_checkpoint(source, optimizer, progress, SimpleNamespace(optimizer_steps=1, steps=17))
    path = tmp_path / "parent.pt"
    cp.save_event_checkpoint(path, source, extra={"event_ppo_training": saved, "kl_coefficient": .037,
        "mixed_deck_controller": {"coefficient": .023, "mixed_round": 200}})
    original_bytes = path.read_bytes()
    loaded = cp.load_event_checkpoint(path)
    migrated, extra = migrate_ability_checkpoint(loaded)
    assert path.read_bytes() == original_bytes
    assert extra["kl_coefficient"] == .037
    assert extra["mixed_deck_controller"]["coefficient"] == .023
    for name, value in source.state_dict().items():
        torch.testing.assert_close(migrated.state_dict()[name], value, atol=0, rtol=0)
    restored, counters = restore_optimizer(migrated, extra)
    assert counters == dict(iterations=1, optimizer_steps=1, decision_rows=17)
    source_parameters, target_parameters = dict(source.named_parameters()), dict(migrated.named_parameters())
    for name, parameter in source_parameters.items():
        old_state, new_state = optimizer.state.get(parameter, {}), restored.state.get(target_parameters[name], {})
        assert old_state.keys() == new_state.keys()
        for key, value in old_state.items():
            torch.testing.assert_close(new_state[key], value, atol=0, rtol=0)
    for name in extra["architecture_migration"]["fresh_parameter_names"]:
        assert target_parameters[name] not in restored.state
    target = tmp_path / "ability.pt"
    cp.save_event_checkpoint(target, migrated, extra=extra)
    with pytest.raises(ValueError, match="finetuning"):
        cp.load_event_checkpoint(target)
    with pytest.warns(UserWarning, match="initialization"):
        resumed = cp.load_event_checkpoint(target, initialization_only=True)
    assert resumed.payload[cp.CONTRACT_KEY]["version"] == "event-checkpoint.v4"
    assert resumed.contract_sha256 != loaded.contract_sha256
    from native_runner.training.event_inference import export_inference, InferenceModel
    from native_runner.training.event_inference_service import Service
    with pytest.raises(ValueError, match="recalibration"):
        export_inference(target, tmp_path / "inference")
    descriptor = export_inference(target, tmp_path / "inference", initialization_only=True)
    inferred, _ = InferenceModel().load(descriptor)
    assert inferred.config.ability_actions
    with pytest.raises(ValueError, match="purpose mismatch"):
        InferenceModel().load({k:v for k,v in descriptor.items() if k != "initialization_only"})
    old_descriptor = export_inference(path, tmp_path / "inference")
    service = Service("cpu")
    result = service.control(dict(operation="load_set", descriptors=[old_descriptor, descriptor]))
    assert len(result["identities"]) == 2
    args = parse_args(["--checkpoint", str(target), "--cache-root", str(tmp_path), "--initialization-only"])
    with pytest.warns(UserWarning, match="initialization"):
        assert UpdateSession(args).loaded.model.config.ability_actions
    # The public completion hook only belongs after actual finetuning.
    cp.complete_event_initialization(resumed.model)
    cp.save_event_checkpoint(target, resumed.model, extra=extra)
    ready = cp.load_event_checkpoint(target)
    assert ready.model.event_initialization["needs_evaluation"]
    with pytest.raises(ValueError, match="already supports"):
        migrate_ability_checkpoint(ready)
    payload = ready.payload
    payload[cp.CONTRACT_KEY]["model_config"]["ability_actions"] = False
    torch.save(payload, tmp_path / "tampered.pt")
    with pytest.raises(ValueError, match="must enable"):
        cp.load_event_checkpoint(tmp_path / "tampered.pt")


def test_migration_cli_verifies_written_checkpoint_and_rejects_overwrite(tmp_path, monkeypatch, capsys):
    from tools import event_migrate_abilities as cli
    source = cp._build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                             EventModelConfigV1(), EventPolicyConfigV1())
    parent, target = tmp_path / "parent.pt", tmp_path / "ability.pt"
    source_digest = cp.save_event_checkpoint(parent, source)
    monkeypatch.setattr(sys, "argv", ["event_migrate_abilities", "--source", str(parent),
                                      "--output", str(target)])
    with pytest.warns(UserWarning, match="initialization"):
        cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["readback_verified"] is True
    assert report["source_checkpoint_sha256"] == source_digest
    with pytest.warns(UserWarning, match="initialization"):
        saved = cp.load_event_checkpoint(target, initialization_only=True)
    assert saved.checkpoint_sha256 == report["sha256"]
    assert saved.model.config.ability_actions
    output_bytes = target.read_bytes()
    with pytest.raises(SystemExit) as rejected:
        cli.main()
    assert rejected.value.code == 2
    assert target.read_bytes() == output_bytes
    assert cp.load_event_checkpoint(parent).checkpoint_sha256 == source_digest
