"""PPO checkpoint boundaries must preserve AdamW's continuous update trajectory."""

from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training import event_checkpoint
from native_runner.training.event_cache import canonicalize_event_observation
from native_runner.training.event_delay_constraints import delay_constraints_contract
from native_runner.training.event_ppo import EventPpoConfigV1, event_ppo_update
from native_runner.training.event_preflight import load_complete_rollout
from native_runner.training.event_rollout import EventLaneRecorderV1
from tools import event_rl_update as cli


def _collect(model, destination, checkpoint_sha, contract_sha):
    recorder = EventLaneRecorderV1(
        match_id="resume-test", policy_version="test", temperatures=(1.,) * 4,
        decks={0: tuple(range(8)), 1: tuple(range(8))},
        behavior_checkpoint_sha256=checkpoint_sha, behavior_model_contract_hash=contract_sha,
        action_constraints=delay_constraints_contract(),
    )
    model.eval()
    with torch.no_grad():
        for index in range(3):
            frame = canonicalize_event_observation(_batch(1))
            context = model.encode(frame.to_model_input("cpu", float_dtype=torch.float32),
                                   model.initial_state(1), episode_start=torch.ones(1, dtype=torch.bool))
            card = torch.ones(1, model.card_count, dtype=torch.bool)
            cell = torch.ones(1, model.card_count, model.cell_count, dtype=torch.bool)
            delay = torch.ones(1, model.card_count, model.event_config.delay_bin_count, dtype=torch.bool)
            delay[:, :, 0] = False
            gate = torch.tensor([[False, True]])
            step = model.select_action(context, card_mask=card, cell_masks=cell,
                                       delay_mask=delay, gate_mask=gate, temperatures=(1.,) * 4)
            recorder.decide(owner=0, tick=100 + index * 10, frame=frame, step=step,
                            card_mask=card, cell_masks=cell, delay_mask=delay, gate_mask=gate)
            from native_runner.tests.test_event_rollout import _confirm
            _confirm(recorder, 0, 100 + index * 10)
            recorder.advance(ticks=10, rewards={0: 1.2} if index == 2 else {}, done=index == 2)
    recorder.to_shard(destination, shard_id="part-000", contract={})


def _assert_tree_equal(actual, expected):
    if isinstance(expected, torch.Tensor):
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
    elif isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _assert_tree_equal(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected):
            _assert_tree_equal(a, b)
    else:
        assert actual == expected


def test_cli_two_rounds_match_continuous_adamw(tmp_path, monkeypatch, capsys):
    torch.manual_seed(91)
    control = _event_model().eval()
    control_optimizer = torch.optim.AdamW(control.parameters(), lr=4e-5)
    monkeypatch.setattr(event_checkpoint, "_build_model", lambda *args: _event_model())
    checkpoint = tmp_path / "initial-il.pt"
    event_checkpoint.save_event_checkpoint(checkpoint, control)

    for iteration in (1, 2):
        loaded = event_checkpoint.load_event_checkpoint(checkpoint)
        cache = tmp_path / f"round-{iteration}"
        cache.mkdir()
        _collect(loaded.model, cache / "part-000.crev.zst", loaded.checkpoint_sha256, loaded.contract_sha256)
        lanes, _ = load_complete_rollout([cache / "part-000.json"])
        event_ppo_update(control, lanes, control_optimizer,
                         config=EventPpoConfigV1(minibatch_lanes=0))

        output = tmp_path / f"updated-{iteration}.pt"
        arguments = ["event_rl_update", "--cache-root", str(cache), "--checkpoint", str(checkpoint),
                     "--output", str(output), "--minibatch-lanes", "0"]
        if iteration == 1:
            arguments.extend(["--lr", "4e-5"])
        else:
            # A recurring launcher flag must not re-warm a resumed critic.
            arguments.extend(["--value-warmup-steps", "1"])
            monkeypatch.setattr(cli, "warm_up_value", lambda *args, **kwargs: pytest.fail("repeated warmup"))
        monkeypatch.setattr(sys, "argv", arguments)
        assert cli.main() == 0
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        lifecycle = next(row for row in events if row["event"] == "optimizer-state")
        assert lifecycle["resumed"] is (iteration == 2)
        assert lifecycle["value_warmup_skipped"] is (iteration == 2)
        assert lifecycle["learning_rates"] == [4e-5]

        saved = event_checkpoint.load_event_checkpoint(output)
        training = saved.payload[cli.PPO_TRAINING_KEY]
        assert training["iterations"] == training["optimizer_steps"] == iteration
        assert training["decision_rows"] == iteration * 3
        assert saved.payload["parent_checkpoint_sha256"] == loaded.checkpoint_sha256
        _assert_tree_equal(saved.model.state_dict(), control.state_dict())
        _assert_tree_equal(training["optimizer"], control_optimizer.state_dict())
        checkpoint = output


def _trained_linear():
    model = torch.nn.Linear(2, 1)
    optimizer, progress = cli.restore_optimizer(model, {"optimizer": {"unrelated_il_state": True}})
    assert not optimizer.state
    model(torch.ones(1, 2)).sum().backward()
    optimizer.step()
    training = cli.optimizer_checkpoint(model, optimizer, progress, SimpleNamespace(optimizer_steps=1, steps=3))
    return model, training


def test_explicit_learning_rate_override_preserves_moments():
    model, training = _trained_linear()
    optimizer, progress = cli.restore_optimizer(model, {cli.PPO_TRAINING_KEY: training}, learning_rate=2e-6)
    assert optimizer.param_groups[0]["lr"] == 2e-6
    assert progress == dict(iterations=1, optimizer_steps=1, decision_rows=3)
    _assert_tree_equal(optimizer.state_dict()["state"], training["optimizer"]["state"])


@pytest.mark.parametrize("change", ["version", "parameter_names", "optimizer_type", "optimizer", "optimizer_steps"])
def test_invalid_resume_state_rejected(change):
    model, training = _trained_linear()
    training = deepcopy(training)
    training[change] = None
    with pytest.raises(ValueError, match="event PPO"):
        cli.restore_optimizer(model, {cli.PPO_TRAINING_KEY: training})
