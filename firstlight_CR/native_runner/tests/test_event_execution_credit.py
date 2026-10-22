"""Native execution evidence gates every learner, not only PPO's actor loss."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_event_rollout import _recorder, _decision, _confirm
from native_runner.tests.test_event_preflight import rollout  # noqa: F401 - shared real-policy fixture
from native_runner.training.event_cache import load_event_rl_shard, write_event_rl_shard
from native_runner.training.event_rollout import lanes_from_shard
from native_runner.training.event_preflight import load_complete_rollout, EventPreflightError, preflight_event_rollout
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.training.event_ppo import (
    EventPpoConfigV1, event_ppo_update, event_lane_advantages,
    warm_up_value, value_head_parameters, rollout_sampled_kl,
)


def _save(recorder, tmp_path, name="part-000"):
    recorder.to_shard(tmp_path / f"{name}.crev.zst", shard_id=name, contract={})
    return tmp_path / f"{name}.json"


@pytest.mark.parametrize("replacement_acts", [False, True])
def test_interrupted_act_is_audited_but_never_becomes_a_training_row(tmp_path, replacement_acts):
    recorder = _recorder()
    _decision(recorder, 0, 100, executed=False)
    recorder.advance(ticks=60)
    _decision(recorder, 0, 160, act=replacement_acts,
              log_prob=-3.0 if replacement_acts else -0.4)
    recorder.advance(ticks=40, rewards={0: 1.0}, done=True)
    summary = _save(recorder, tmp_path)
    shard = load_event_rl_shard(summary)
    assert shard.records[0].act and shard.records[0].execution_status == "cancelled"
    assert shard.records[0].exclusion_reason == "interrupted"
    assert shard.summary["trainable_frames"] == shard.summary["excluded_frames"] == 1
    metadata = []
    lanes, counts = load_complete_rollout([summary], row_metadata=metadata)
    assert counts["rows"] == 2 and counts["trainable_rows"] == counts["excluded_rows"] == 1
    assert lanes[0].time_steps == 1 and lanes[0].reward.tolist() == [1.0]
    assert lanes[0].actions.act.tolist() == [replacement_acts]
    assert [row["observation_tick"] for row in metadata[0]] == [160]


@pytest.mark.parametrize("event_type, trainable", [
    ("action_executed", True), ("action_rejected", False), ("action_canceled_terminal", False),
])
@pytest.mark.parametrize("receipt_after_close", [False, True])
def test_receipt_is_bound_to_original_decision_even_after_terminal_close(
    tmp_path, event_type, trainable, receipt_after_close,
):
    recorder = _recorder()
    _decision(recorder, 0, 100, executed=False)
    recorder.submit(0, tick=100, action_id="native:100")
    outcomes = [SimpleNamespace(owner=0, data={"action_id": "native:100"}, event_type=event_type)]
    if not receipt_after_close:
        recorder.reconcile(outcomes)
    recorder.advance(ticks=1, rewards={0: 1.0}, done=True)
    if receipt_after_close:
        assert not recorder.rows[0][0].trainable
        recorder.reconcile(outcomes)
    shard = load_event_rl_shard(_save(recorder, tmp_path))
    assert shard.records[0].trainable is trainable
    assert len(lanes_from_shard(shard)) == int(trainable)


def test_late_receipt_never_marks_replacement_as_executed():
    recorder = _recorder()
    _decision(recorder, 0, 100, executed=False)
    recorder.submit(0, tick=100, action_id="old")
    recorder.advance(ticks=2)
    _decision(recorder, 0, 102, executed=False)
    recorder.reconcile([SimpleNamespace(owner=0, data={"action_id": "old"}, event_type="action_executed")])
    recorder.advance(ticks=3, done=True)
    assert [row.execution_status for row in recorder.rows[0]] == ["executed", "cancelled"]


def test_excluded_tail_keeps_terminal_reward_and_elapsed_ticks(tmp_path):
    recorder = _recorder()
    _decision(recorder, 0, 100)
    recorder.advance(ticks=10)
    _decision(recorder, 0, 110, executed=False)
    recorder.advance(ticks=40, rewards={0: 1.0}, done=True)
    lanes, _ = load_complete_rollout([_save(recorder, tmp_path)])
    lane = lanes[0]
    assert lane.time_steps == 1 and lane.decision_ticks.tolist() == [50]
    assert lane.reward.tolist() == [1.0] and lane.done.tolist() == [True]
    advantage, target = event_lane_advantages(lane, torch.zeros(1), gamma=.999, gae_lambda=.95)
    assert advantage.tolist() == target.tolist() == [1.0]


def test_queue_submission_is_not_execution_and_empty_lane_is_skipped(tmp_path):
    recorder = _recorder()
    _decision(recorder, 0, 100, executed=False)
    recorder.submit(0, tick=100, action_id="queued-only")
    _decision(recorder, 1, 100, act=False, log_prob=-0.4)
    recorder.advance(ticks=20, done=True, rewards={0: 1., 1: -1.})
    metadata = []
    lanes, counts = load_complete_rollout([_save(recorder, tmp_path)], row_metadata=metadata)
    assert recorder.rows[0][0].execution_status == "unconfirmed"
    assert counts["empty_training_lanes"] == 1 and counts["training_lanes"] == 1
    assert len(lanes) == len(metadata) == 1
    assert metadata[0][0]["owner"] == 1
    assert lanes[0].reward.tolist() == [-1.]


def test_all_excluded_rejects_before_any_optimizer_and_legacy_is_audit_only(tmp_path):
    recorder = _recorder()
    _decision(recorder, 0, 100, executed=False)
    recorder.advance(ticks=10, done=True, rewards={0: 1.0})
    summary = _save(recorder, tmp_path)
    with pytest.raises(EventPreflightError, match="no trainable") as error:
        load_complete_rollout([summary])
    assert error.value.metrics["empty_training_lanes"] == 1
    shard = load_event_rl_shard(summary)
    shard.records = tuple(replace(row, execution_status="legacy_unknown") for row in shard.records)
    with pytest.raises(ValueError, match="legacy rollout"):
        lanes_from_shard(shard)
    _rewrite(shard, summary, list(shard.records), [0])
    with pytest.raises(EventPreflightError, match="execution evidence"):
        load_complete_rollout([summary])


def _rewrite(shard, summary, records, indices):
    write_event_rl_shard(summary.with_name(summary.stem + ".crev.zst"), records,
        [shard.observation(i) for i in indices], shard_id=summary.stem,
        contract=shard.manifest["contract"], masks=[
            (shard.card_masks[i], shard.cell_masks[i], shard.delay_masks[i], shard.gate_masks[i])
            for i in indices])


@pytest.mark.parametrize("prepared_mode", [False, True])
def test_excluded_rows_have_zero_influence_on_full_update_warmup_and_kl(rollout, tmp_path, prepared_mode):
    model, summary = rollout
    shard = load_event_rl_shard(summary)
    rows = list(shard.records)
    for i in (0, 2, 4):
        rows[i] = replace(rows[i], execution_status="cancelled", exclusion_reason="interrupted",
                          log_prob=-10000., card_row=9999, cell=9999, delay_bin=9999)
    _rewrite(shard, summary, rows, list(range(5)))
    # Independent expected effective lane: WAIT at 110, executed ACT at 130.
    control_summary = tmp_path / "control.json"
    control_rows = [replace(rows[1], decision_ticks=20),
                    replace(rows[3], decision_ticks=20, reward=rows[4].reward, done=True)]
    _rewrite(shard, control_summary, control_rows, [1, 3])
    full, counts = load_complete_rollout([summary])
    control, _ = load_complete_rollout([control_summary])
    assert counts["trainable_rows"] == 2 and counts["excluded_rows"] == 3
    assert full[0].decision_ticks.tolist() == [20, 20]
    from native_runner.tests.test_event_model import _event_model
    models = [_event_model(), _event_model()]
    for current in models:
        current.load_state_dict(model.state_dict())
    states, statistics = [], []
    config = EventPpoConfigV1(epochs=2, minibatch_lanes=0, sequence_chunk_steps=1,
        kl_coefficient=.2, bc_coefficient=.3, target_kl=1.0)
    for current, lanes in zip(models, [full, control]):
        warm = torch.optim.AdamW([p for _, p in value_head_parameters(current)], lr=1e-4)
        warm_up_value(current, lanes, warm, steps=1, gamma=.999, chunk_steps=1, minibatch_lanes=1)
        prepared = PreparedEventRollout(current, lanes, row_batch_size=1) if prepared_mode else None
        # Warmup can alter shared policy context; PPO preflight/value reuse is
        # separately exercised with the unchanged behavior checkpoint.
        behavior_prepared = PreparedEventRollout(model, lanes, row_batch_size=1) if prepared_mode else None
        assert preflight_event_rollout(model, lanes, prepared=behavior_prepared).rows == 2
        optimizer = torch.optim.AdamW(current.parameters(), lr=1e-5)
        stats = event_ppo_update(current, lanes, optimizer, config=config,
                                 reference_model=model, prepared=prepared)
        assert stats.steps == 2 and stats.optimizer_steps == 2
        statistics.append(stats.as_dict())
        states.append(deepcopy(current.state_dict()))
    assert statistics[0] == statistics[1]
    for name in states[0]:
        torch.testing.assert_close(states[0][name], states[1][name], rtol=0, atol=0)
    assert rollout_sampled_kl(models[0], full, chunk_steps=1) == rollout_sampled_kl(models[1], control, chunk_steps=1)
