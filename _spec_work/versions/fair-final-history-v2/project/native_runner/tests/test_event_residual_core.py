from dataclasses import replace

import pytest
import torch

from native_runner.training.event_checkpoint import (
    _build_model, load_event_checkpoint, save_event_checkpoint,
)
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_residual_core import StatelessGatedResidualCore, active_gate_rows
from native_runner.training.event_residual_migration import migrate_residual_checkpoint
from native_runner.training.v4.config import scaled_model_config_v4
from tools.event_rl_update import restore_optimizer


def test_core_preserves_zero_state_function_and_has_no_memory():
    torch.manual_seed(923)
    old = torch.nn.LSTMCell(13, 17)
    new = StatelessGatedResidualCore(old, blocks=2, residual_hidden=23)
    x = torch.randn(7, 13)
    zeros = (torch.zeros(7, 17), torch.zeros(7, 17))
    expected = old(x, zeros)
    got = new(x, zeros)
    for a, b in zip(expected, got):
        torch.testing.assert_close(a, b, atol=1e-6, rtol=1e-6)
    changed = new(x, (torch.randn(7, 17), torch.randn(7, 17)))
    for a, b in zip(got, changed):
        torch.testing.assert_close(a, b, atol=0, rtol=0)


def test_residual_learns_after_identity_initialization():
    torch.manual_seed(923)
    core = StatelessGatedResidualCore(torch.nn.LSTMCell(13, 17), blocks=2, residual_hidden=23)
    x = torch.randn(7, 13)
    state = (torch.zeros(7, 17), torch.zeros(7, 17))
    goal = torch.randn(7, 17)
    optimizer = torch.optim.AdamW(core.parameters(), lr=1e-3)
    for _ in range(2):
        optimizer.zero_grad()
        (core(x, state)[0] - goal).square().mean().backward()
        assert all(block.output.weight.grad.abs().sum() > 0 for block in core.residual)
        optimizer.step()
    assert all(block.input.weight.grad.abs().sum() > 0 for block in core.residual)


def test_migration_preserves_adamw_and_strict_roundtrip(tmp_path):
    source = _build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                          EventModelConfigV1(delay_hidden=32, cell_hidden=16), EventPolicyConfigV1())
    names = [n for n, _ in source.named_parameters()]
    optimizer = torch.optim.AdamW(source.parameters(), lr=1e-5)
    # Distinct nonzero moments expose order or gate-slice errors.
    for index, (name, p) in enumerate(source.named_parameters()):
        if name.startswith(("backbone.lstm_core.", "card_query.")):
            optimizer.state[p] = {"step": torch.tensor(171.),
                                  "exp_avg": torch.arange(p.numel()).reshape(p.shape).float() * 1e-7,
                                  "exp_avg_sq": torch.full_like(p, (index + 1) * 1e-6)}
    progress = dict(version="event-ppo-training.v1", optimizer_type="AdamW", parameter_names=names,
                    optimizer=optimizer.state_dict(), iterations=171, optimizer_steps=171, decision_rows=646830)
    path = tmp_path / "old.pt"
    save_event_checkpoint(path, source, extra={"event_ppo_training": progress})
    loaded = load_event_checkpoint(path)
    assert loaded.payload["event_checkpoint_contract"]["version"] == "event-checkpoint.v2"
    target, extra = migrate_residual_checkpoint(loaded, blocks=2, hidden=32)
    migrated = tmp_path / "new.pt"
    save_event_checkpoint(migrated, target, extra=extra)
    restored = load_event_checkpoint(migrated)
    assert restored.payload["event_checkpoint_contract"]["version"] == "event-checkpoint.v3"
    assert restored.contract_sha256 != loaded.contract_sha256
    assert restored.model.config.residual_blocks == 2
    opt, counts = restore_optimizer(restored.model, restored.payload)
    assert counts == dict(iterations=171, optimizer_steps=171, decision_rows=646830)
    old_parameters = dict(source.named_parameters())
    rows = active_gate_rows(source.backbone.config.lstm_hidden_dim)
    for name, p in restored.model.named_parameters():
        if name.startswith("backbone.lstm_core.residual."):
            assert p not in opt.state
        elif name in extra["architecture_migration"]["sliced_parameter_names"]:
            for key in ("exp_avg", "exp_avg_sq"):
                torch.testing.assert_close(opt.state[p][key], optimizer.state[old_parameters[name]][key][rows])
        elif name.startswith("card_query."):
            for key in ("exp_avg", "exp_avg_sq", "step"):
                torch.testing.assert_close(opt.state[p][key], optimizer.state[old_parameters[name]][key])
    # Loading v2 remains byte-semantically stable, including its contract hash.
    again = load_event_checkpoint(path)
    assert again.contract_sha256 == loaded.contract_sha256


def test_residual_requires_snapshot_mode():
    with pytest.raises(ValueError, match="snapshot_only"):
        _build_model(scaled_model_config_v4(.125), EventModelConfigV1(residual_blocks=1), EventPolicyConfigV1())
