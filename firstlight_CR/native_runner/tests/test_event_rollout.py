"""Credit-assignment tests for the rollout recorder, engine-free."""

from __future__ import annotations

import pytest
import torch

from native_runner.training.event_model import EventActionV1, EventStepV1
from native_runner.training.event_rollout import EventLaneRecorderV1
from native_runner.tests.test_training_v4_model import _batch

TEMPERATURES = (1.0, 1.0, 1.0, 1.0)
DECKS = {0: (1, 2, 3, 4, 5, 6, 7, 8), 1: (9, 10, 11, 12, 13, 14, 15, 16)}


def _step(*, act: bool = True, log_prob: float = -3.0) -> EventStepV1:
    scalar = lambda value: torch.tensor([value], dtype=torch.float32)
    return EventStepV1(
        action=EventActionV1(
            act=torch.tensor([act]),
            card_row=torch.tensor([2 if act else -1]),
            cell=torch.tensor([13 if act else -1]),
            delay_bin=torch.tensor([4 if act else -1]),
        ),
        log_prob=scalar(log_prob),
        gate_log_prob=scalar(-0.4),
        card_log_prob=scalar(-1.2 if act else 0.0),
        cell_log_prob=scalar(-1.1 if act else 0.0),
        delay_log_prob=scalar(-0.3 if act else 0.0),
        gate_entropy=scalar(0.6),
        card_entropy=scalar(1.5 if act else 0.0),
        cell_entropy=scalar(5.0 if act else 0.0),
        delay_entropy=scalar(3.0 if act else 0.0),
        temperatures=TEMPERATURES,
    )


def _recorder() -> EventLaneRecorderV1:
    return EventLaneRecorderV1(
        match_id="m-1",
        policy_version="ckpt-a",
        temperatures=TEMPERATURES,
        decks=DECKS,
    )


def _confirm(recorder, owner, tick, event_type="action_executed"):
    from types import SimpleNamespace
    action_id = f"{owner}:{tick}"
    recorder.submit(owner, tick=tick, action_id=action_id)
    recorder.reconcile([SimpleNamespace(owner=owner, data={"action_id": action_id}, event_type=event_type)])


def _decision(recorder, owner: int, tick: int, *, act: bool = True, log_prob: float = -3.0,
              executed: bool = True) -> None:
    frame = _batch(1)
    recorder.decide(
        owner=owner,
        tick=tick,
        frame=frame,
        step=_step(act=act, log_prob=log_prob),
        card_mask=torch.ones(1, 8, dtype=torch.bool),
        cell_masks=torch.ones(1, 8, 576, dtype=torch.bool),
        delay_mask=torch.ones(1, 8, 61, dtype=torch.bool),
        gate_mask=torch.ones(1, 2, dtype=torch.bool),
    )
    if act and executed:
        _confirm(recorder, owner, tick)


def test_transition_runs_until_the_same_actor_decides_again() -> None:
    recorder = _recorder()
    _decision(recorder, 0, 200)
    recorder.advance(ticks=10)
    _decision(recorder, 0, 210)          # closes the first row at exactly 10 ticks
    recorder.advance(ticks=14)
    recorder.finish(truncated=True)

    rows = recorder.rows[0]
    assert len(rows) == 2
    assert [row.decision_ticks for row in rows] == [10, 14]
    # Closing a row because the actor decided again is not an episode end: the
    # GAE trace has to survive every non-terminal boundary.
    assert [row.done for row in rows] == [False, False]
    assert rows[-1].truncated
    assert rows[0].observation_tick == 200 and rows[1].observation_tick == 210


def test_only_the_terminal_reward_lands_and_only_on_the_final_row() -> None:
    recorder = _recorder()
    _decision(recorder, 0, 200)
    recorder.advance(ticks=12)
    _decision(recorder, 0, 212)
    recorder.advance(ticks=8, rewards={0: 1.4, 1: -1.4}, done=True)

    rows = recorder.rows[0]
    assert [row.reward for row in rows] == [0.0, 1.4]
    assert [row.done for row in rows] == [False, True]
    assert rows[-1].decision_ticks == 8
    assert recorder.results() == {0: 1.4, 1: -1.4}
    assert not recorder.rows[1]     # the opponent never decided: no lane for it


def test_each_owner_keeps_its_own_lane() -> None:
    """Mirror self-play shares one policy, but credit must not jump between the
    two perspectives inside a lane."""

    recorder = _recorder()
    _decision(recorder, 0, 200)
    recorder.advance(ticks=5)
    _decision(recorder, 1, 205)
    recorder.advance(ticks=5)
    _decision(recorder, 0, 210)
    recorder.advance(ticks=5, rewards={0: 0.0, 1: 1.4}, done=True)

    assert [row.decision_ticks for row in recorder.rows[0]] == [10, 5]
    assert [row.decision_ticks for row in recorder.rows[1]] == [10]
    assert [row.owner for row in recorder.rows[1]] == [1]
    assert recorder.rows[1][0].reward == 1.4
    assert recorder.rows[1][0].deck == DECKS[1]
    assert recorder.rows[1][0].opponent_deck == DECKS[0]
    assert recorder.results() == {0: 0.0, 1: 1.4}


def test_shard_round_trip_carries_both_perspectives(tmp_path) -> None:
    from native_runner.training.event_cache import load_event_rl_shard

    recorder = _recorder()
    _decision(recorder, 0, 200)
    _decision(recorder, 1, 200)
    recorder.advance(ticks=9, rewards={0: 1.4, 1: -1.4}, done=True)

    summary = recorder.to_shard(
        tmp_path / "shard.crev.zst",
        shard_id="shard",
        contract={"version": "test"},
    )
    assert summary["frames"] == 2
    assert summary["terminal_frames"] == 2
    shard = load_event_rl_shard(tmp_path / "shard.json", verify=True)
    assert {row.owner for row in shard.records} == {0, 1}
    assert all(row.decision_ticks == 9 for row in shard.records)
    assert shard.observation(0).batch_size == 1


def test_open_rows_are_visible_before_they_close() -> None:
    recorder = _recorder()
    _decision(recorder, 0, 200)
    assert recorder.open_rows == 1
    assert recorder.rows[0] == []      # nothing is written until it closes
    recorder.advance(ticks=3)
    assert recorder.open_rows == 1
    recorder.finish(truncated=True)
    assert recorder.open_rows == 0
    assert len(recorder.rows[0]) == 1
    assert recorder.rows[0][0].decision_ticks == 3


def test_recorded_shard_becomes_lanes_the_update_consumes(tmp_path) -> None:
    """The whole P0 loop, engine-free: record decisions, write a shard, rebuild
    both perspectives' lanes from it, and run a PPO iteration on them."""

    import math

    from native_runner.training.event_cache import load_event_rl_shard
    from native_runner.training.event_ppo import EventPpoConfigV1, event_ppo_update
    from native_runner.training.event_rollout import lanes_from_shard
    from native_runner.tests.test_event_model import _event_model

    model = _event_model()
    model.eval()
    recorder = _recorder()
    for tick in (200, 210):
        _decision(recorder, 0, tick)
        _decision(recorder, 1, tick)
        recorder.advance(ticks=5)
    recorder.advance(ticks=5, rewards={0: 1.4, 1: -1.4}, done=True)

    recorder.to_shard(tmp_path / "shard.crev.zst", shard_id="shard", contract={"version": "test"})
    shard = load_event_rl_shard(tmp_path / "shard.json", verify=True)
    assert shard.card_masks is not None and shard.cell_masks is not None
    assert tuple(shard.card_masks.shape) == (4, 8)
    assert tuple(shard.cell_masks.shape) == (4, 8, 576)

    lanes = lanes_from_shard(shard)
    assert [lane.time_steps for lane in lanes] == [2, 2]
    # Only the final row of each lane ends the episode, so GAE keeps its trace.
    assert [row for lane in lanes for row in lane.done.tolist()] == [False, True, False, True]

    optimizer = torch.optim.SGD(model.parameters(), lr=1e-4)
    stats = event_ppo_update(model, lanes, optimizer, config=EventPpoConfigV1(epochs=1))
    assert stats.lanes == 2 and stats.steps == 4
    assert math.isfinite(stats.policy_loss) and math.isfinite(stats.approx_kl)
    # A shard without masks cannot be rebuilt into lanes: the exact gap that
    # would otherwise show up as a silently wrong re-score.
    from native_runner.training.event_cache import write_event_rl_shard

    write_event_rl_shard(
        tmp_path / "nomask.crev.zst",
        list(shard.records),
        [shard.observation(index) for index in range(shard.frames)],
        shard_id="nomask",
        contract={"version": "test"},
    )
    maskless = load_event_rl_shard(tmp_path / "nomask.json")
    with pytest.raises(ValueError, match="legality masks"):
        lanes_from_shard(maskless)
