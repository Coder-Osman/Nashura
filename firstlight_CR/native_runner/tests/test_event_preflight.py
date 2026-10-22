"""Real shard round trips and the mandatory pre-update checkpoint gate."""

from dataclasses import replace
import json
import sys

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training import event_checkpoint
from native_runner.training.event_checkpoint import event_checkpoint_payload, save_event_checkpoint
from native_runner.training.event_cache import load_event_rl_shard, write_event_rl_shard
from native_runner.training.event_delay_constraints import delay_constraints_contract
from native_runner.training.event_preflight import (
    EventPreflightError, load_complete_rollout, preflight_event_rollout,
)
from native_runner.training.event_rollout import EventLaneRecorderV1
from tools import event_rl_update as cli

TEMPERATURES = (0.7, 1.6, 0.8, 1.4)


@pytest.fixture
def rollout(tmp_path):
    torch.manual_seed(42)
    model = _event_model().eval()
    recorder = EventLaneRecorderV1(
        match_id="real-container", policy_version="checkpoint",
        temperatures=TEMPERATURES, decks={0: tuple(range(8)), 1: tuple(range(8))},
    )
    with torch.no_grad():
        for i in range(5):
            frame = _batch(1)
            # Exercise actual FP16 rounding rather than integer-valued inputs.
            frame.match_scalars.add_(0.012345)
            context = model.encode(frame.to_model_input("cpu", float_dtype=torch.float32),
                                   model.initial_state(1), episode_start=torch.ones(1, dtype=torch.bool))
            card = torch.zeros(1, model.card_count, dtype=torch.bool)
            card[:, :3] = True
            cell = torch.zeros(1, model.card_count, model.cell_count, dtype=torch.bool)
            cell[:, :, :17] = True
            delay = torch.zeros(1, model.card_count, model.event_config.delay_bin_count, dtype=torch.bool)
            delay[:, :, 2:8] = True
            gate = torch.tensor([[False, True]]) if i != 1 else torch.tensor([[True, False]])
            step = model.select_action(context, card_mask=card, cell_masks=cell,
                                       delay_mask=delay, gate_mask=gate, temperatures=TEMPERATURES)
            recorder.decide(owner=0, tick=100 + 10 * i, frame=frame, step=step,
                            card_mask=card, cell_masks=cell, delay_mask=delay, gate_mask=gate)
            if bool(step.action.act[0]):
                from native_runner.tests.test_event_rollout import _confirm
                _confirm(recorder, 0, 100 + 10 * i)
            recorder.advance(ticks=10, rewards={0: 1.4} if i == 4 else {}, done=i == 4)
    recorder.to_shard(tmp_path / "part-000.crev.zst", shard_id="part-000", contract={})
    return model, tmp_path / "part-000.json"


def rewrite(summary, transform):
    shard = load_event_rl_shard(summary, verify=True)
    records = transform(list(shard.records))
    write_event_rl_shard(
        shard.path, records, list(shard.observations()), shard_id="part-000", contract={},
        masks=list(zip(shard.card_masks, shard.cell_masks, shard.delay_masks, shard.gate_masks)),
    )


def test_unchanged_checkpoint_accepts_all_fp16_rows_and_restores_model(rollout, tmp_path):
    model, summary = rollout
    checkpoint = tmp_path / "checkpoint.pt"
    torch.save(model.state_dict(), checkpoint)
    restored = _event_model()
    restored.load_state_dict(torch.load(checkpoint, weights_only=True))
    before = {key: value.clone() for key, value in restored.state_dict().items()}
    lanes, counts = load_complete_rollout([summary])
    stats = preflight_event_rollout(restored, lanes, chunk_steps=2)
    assert stats.passed and stats.rows == stats.finite_rows == counts["rows"] == 5
    assert stats.clip_fraction == 0
    assert stats.ratio_mean == pytest.approx(1, abs=0.001)
    assert stats.max_abs_log_ratio <= 0.01
    assert stats.mean_abs_log_ratio <= 0.001
    assert restored.training
    assert all(torch.equal(value, before[key]) for key, value in restored.state_dict().items())
    assert all(parameter.grad is None for parameter in restored.parameters())


def test_changed_policy_rejected(rollout):
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    with torch.no_grad():
        model.delay_head[-1].bias[2:8].add_(torch.arange(6) * 3)
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, lanes, chunk_steps=2)
    assert not error.value.metrics["passed"]
    assert error.value.metrics["rows"] == 5
    assert error.value.metrics["max_abs_log_ratio"] > 0.01
    assert not model.training


@pytest.mark.parametrize("field", ["gate_mask", "card_mask", "cell_masks", "delay_mask", "temperatures"])
def test_every_stored_mask_and_temperature_is_honored(rollout, field):
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    assert preflight_event_rollout(model, lanes).passed
    lane = lanes[0]
    changed = (1.0, 1.0, 1.0, 1.0) if field == "temperatures" else torch.ones_like(getattr(lane, field))
    with pytest.raises(EventPreflightError):
        preflight_event_rollout(model, [replace(lane, **{field: changed})])


@pytest.mark.parametrize("delta", [0.02, 0.002, float("nan"), float("inf")])
def test_logprob_mismatch_and_nonfinite_fail_closed(rollout, delta):
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    lane = lanes[0]
    old = lane.old_log_prob.clone()
    old += delta
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, [replace(lane, old_log_prob=old)], chunk_steps=2)
    assert error.value.metrics["rows"] == 5
    if not torch.isfinite(torch.tensor(delta)):
        assert error.value.metrics["nonfinite_rows"] == 5
    json.dumps(error.value.metrics, allow_nan=False)


def test_clipping_rejects_even_with_explicitly_wide_logratio_tolerances(rollout):
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    lane = replace(lanes[0], old_log_prob=lanes[0].old_log_prob - 0.3)
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, [lane], max_abs_log_ratio=1, mean_abs_log_ratio=1)
    assert error.value.metrics["clip_fraction"] == 1


def test_nonfinite_current_score_fails_and_restores_mode(rollout, monkeypatch):
    from types import SimpleNamespace
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    model.train()
    monkeypatch.setattr(model, "evaluate_actions", lambda context, action, **kwargs:
                        SimpleNamespace(log_prob=torch.full_like(action.act, float("nan"), dtype=torch.float32)))
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, lanes, chunk_steps=2)
    assert error.value.metrics["nonfinite_rows"] == 5
    assert model.training


def test_max_error_checks_last_row_of_last_shard(rollout, tmp_path):
    model, summary = rollout
    shard = load_event_rl_shard(summary)
    second = tmp_path / "part-001.crev.zst"
    records = list(shard.records)
    records[-1] = replace(records[-1], log_prob=records[-1].log_prob - 0.02)
    write_event_rl_shard(second, records, list(shard.observations()), shard_id="part-001", contract={},
                         masks=list(zip(shard.card_masks, shard.cell_masks, shard.delay_masks, shard.gate_masks)))
    lanes, _ = load_complete_rollout([summary, tmp_path / "part-001.json"])
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, lanes, chunk_steps=2, mean_abs_log_ratio=0.01)
    assert error.value.metrics["rows"] == 10
    assert error.value.metrics["max_abs_log_ratio"] > 0.01


@pytest.mark.parametrize("kind", ["incomplete", "truncated", "temperatures", "policy"])
def test_rejects_partial_or_inconsistent_lanes_with_counts(rollout, kind):
    _, summary = rollout
    def transform(records):
        changes = {"done": False} if kind == "incomplete" else (
            {"done": False, "truncated": True} if kind == "truncated" else (
                {"temperatures": (1., 1., 1., 1.)} if kind == "temperatures" else {"policy_version": "other"}))
        records[-1] = replace(records[-1], **changes)
        return records
    rewrite(summary, transform)
    with pytest.raises(EventPreflightError) as error:
        load_complete_rollout([summary])
    counts = error.value.metrics
    assert counts["rejected_rows"] == 5 and counts["rejected_lanes"] == 1
    assert counts["complete_lanes"] == 0
    assert counts["truncated_lanes"] == int(kind == "truncated")
    assert counts["incomplete_lanes"] == int(kind in ("incomplete", "truncated"))
    assert counts["inconsistent_lanes"] == int(kind in ("temperatures", "policy"))


def bind_cli_checkpoint(monkeypatch, checkpoint, model, summary):
    checkpoint_hash = save_event_checkpoint(checkpoint, model)
    identity = dict(behavior_checkpoint_sha256=checkpoint_hash,
                    behavior_model_contract_hash=event_checkpoint_payload(model)["contract_sha256"])
    shard = load_event_rl_shard(summary)
    write_event_rl_shard(
        shard.path, [replace(row, **identity) for row in shard.records],
        list(shard.observations()), shard_id="part-000",
        contract={**identity, "action_constraints": delay_constraints_contract()},
        masks=list(zip(shard.card_masks, shard.cell_masks, shard.delay_masks, shard.gate_masks)),
    )
    # Only substitute the tiny test catalog/model factory; exercise real loading.
    monkeypatch.setattr(event_checkpoint, "_build_model", lambda *args: _event_model())


def setup_cli(monkeypatch, tmp_path, model, summary, extra=()):
    checkpoint = tmp_path / "checkpoint.pt"
    bind_cli_checkpoint(monkeypatch, checkpoint, model, summary)
    output = tmp_path / "must-not-save.pt"
    monkeypatch.setattr(sys, "argv", ["event_rl_update", "--cache-root", str(summary.parent),
                                     "--checkpoint", str(checkpoint), "--output", str(output),
                                     "--value-warmup-steps", "1", *extra])
    def forbidden(*args, **kwargs):
        pytest.fail("optimizer/warmup/update/save reached before successful preflight")
    monkeypatch.setattr(torch.optim, "Adam", forbidden)
    monkeypatch.setattr(torch.optim, "AdamW", forbidden)
    monkeypatch.setattr(cli, "warm_up_value", forbidden)
    monkeypatch.setattr(cli, "event_ppo_update", forbidden)
    monkeypatch.setattr(torch, "save", forbidden)
    return output


@pytest.mark.parametrize("failure", ["checkpoint", "incomplete", "temperature"])
def test_cli_failure_precedes_warmup_optimizer_and_save(rollout, tmp_path, monkeypatch, capsys, failure):
    model, summary = rollout
    extra = ()
    if failure == "checkpoint":
        with torch.no_grad():
            model.delay_head[-1].bias[2:8].add_(torch.arange(6) * 3)
    elif failure == "incomplete":
        rewrite(summary, lambda rows: rows[:-1] + [replace(rows[-1], done=False)])
    else:
        extra = ("--expected-temperatures", "1,1,1,1")
    output = setup_cli(monkeypatch, tmp_path, model, summary, extra)
    with pytest.raises(SystemExit, match="event preflight failed"):
        cli.main()
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[-1]["passed"] is False
    assert messages[-1]["rows"] == 5
    assert not output.exists()


def test_cli_success_preflights_then_warms_updates_and_saves(rollout, tmp_path, monkeypatch, capsys):
    model, summary = rollout
    checkpoint = tmp_path / "checkpoint.pt"
    output = tmp_path / "updated.pt"
    bind_cli_checkpoint(monkeypatch, checkpoint, model, summary)
    monkeypatch.setattr(sys, "argv", ["event_rl_update", "--cache-root", str(summary.parent),
                                     "--checkpoint", str(checkpoint), "--output", str(output),
                                     "--value-warmup-steps", "1", "--value-warmup-lanes", "1",
                                     "--minibatch-lanes", "1"])
    assert cli.main() == 0
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events == ["checkpoint-loaded", "rollout-validation", "rollout-prepared", "event-preflight", "optimizer-state",
                      "value-warmup", "rollout-cache-used", "ppo-update", "update-timings", "saved"]
    assert torch.load(output, weights_only=False)["rl_steps"] == 5


def test_cli_preflight_only_scores_real_shard_without_update_or_save(rollout, tmp_path, monkeypatch, capsys):
    model, summary = rollout
    output = setup_cli(monkeypatch, tmp_path, model, summary, ("--preflight-only",))
    assert cli.main() == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[-1]["event"] == "event-preflight"
    assert messages[-1]["passed"] is True
    assert messages[-1]["rows"] == 5
    assert messages[-1]["complete_lanes"] == 1
    assert not output.exists()
