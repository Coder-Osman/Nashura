from copy import deepcopy
import json

import pytest
import torch

from native_runner.tests.test_fair_final_episode import IDENTITY
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_fair_final_runtime import TickEnvironment
from native_runner.training.fair_final.episode import AcceptanceRecorder
from native_runner.training.fair_final.native import FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction


def reject_one(rollout, owner):
    environment = rollout.runtime.environment
    environment.rejected = {owner}
    actions = {owner: FullAction(11, 0, 1), 1 - owner: FullAction()}
    rollout.commit_simultaneous(actions)
    opponent = rollout.runtime.clock.pending[1 - owner]
    rollout.advance()
    outcomes = rollout.advance()
    assert outcomes == {owner: "rejected"}
    assert set(rollout.freeze_due_information()) == {owner}
    assert rollout.runtime.clock.pending == {1 - owner: opponent}
    environment.rejected.clear()
    return opponent


@pytest.mark.parametrize("owner", [0, 1])
def test_single_ready_commit_preserves_opponent_reservation(rollout, owner):
    opponent = reject_one(rollout, owner)
    before = rollout.runtime.clock.capture()
    with pytest.raises(ValueError, match="atomically"):
        rollout.commit_simultaneous({0: FullAction(), 1: FullAction()})
    assert rollout.runtime.clock.capture() == before
    rollout.commit_simultaneous({owner: FullAction()})
    assert rollout.runtime.clock.pending[1 - owner] is opponent
    assert rollout.runtime.clock.pending[owner].due_tick == 162
    for tick in range(103, 160):
        rollout.advance()
        assert not rollout.freeze_due_information()
        assert rollout.runtime.clock.pending[1 - owner] is opponent
    rollout.advance()
    assert set(rollout.freeze_due_information()) == {1 - owner}
    own_wait = rollout.runtime.clock.pending[owner]
    rollout.commit_simultaneous({1 - owner: FullAction()})
    rollout.advance()
    assert rollout.runtime.clock.pending[owner] is own_wait
    rollout.advance()
    assert set(rollout.freeze_due_information()) == {owner}
    assert rollout.runtime.clock.pending[1 - owner].due_tick == 220
    assert [(event["owner"], event["tick"], event["reason"])
        for event in rollout.runtime.clock.evidence] == [
            (owner, 102, "rejected"), (1 - owner, 160, "wait_expired"),
            (owner, 162, "wait_expired")]


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("delay", [1, 29])
def test_single_ready_public_play_distinguishes_early_and_same_tick(rollout, owner, delay):
    opponent = reject_one(rollout, owner)
    rollout.commit_simultaneous({owner: FullAction(11, 0, delay)})
    due = rollout.runtime.clock.pending[owner].due_tick
    while rollout.runtime.clock.tick < due:
        assert rollout.runtime.clock.pending[1 - owner] is opponent
        rollout.advance()
    assert set(rollout.freeze_due_information()) == {0, 1}
    assert not rollout.runtime.clock.pending
    outcomes = {event["owner"]: event["reason"] for event in rollout.runtime.clock.evidence[-2:]}
    assert outcomes == {owner: "executed", 1 - owner: "preempted" if delay == 1 else "wait_expired"}
    assert all(observation.executed[-1] == (owner, due) for observation in rollout.runtime.frozen.values())


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("action", [FullAction(), FullAction(11, 0, 1), FullAction(11, 0, 29)])
def test_single_ready_snapshot_restores_frames_masks_rng_and_trajectory(rollout, owner, action):
    reject_one(rollout, owner)
    original_clock = rollout.runtime.clock.capture()
    original_information = {seat: info.sha256 for seat, info in rollout.freeze_due_information().items()}
    original_rng = {seat: generator.get_state().clone() for seat, generator in rollout.generators.items()}
    branches = []
    with rollout.snapshot(None) as snapshot:
        for attempt in range(2):
            snapshot.restore()
            assert rollout.runtime.clock.capture() == original_clock
            assert {seat: info.sha256 for seat, info in rollout.freeze_due_information().items()} == original_information
            for seat, generator in rollout.generators.items():
                assert torch.equal(generator.get_state(), original_rng[seat])
            random_values = torch.rand(4, generator=rollout.generators[owner]).tolist()
            rollout.commit_simultaneous({owner: action})
            while not rollout.runtime.frozen:
                rollout.advance()
            assert torch.equal(rollout.generators[1 - owner].get_state(), original_rng[1 - owner])
            branches.append(dict(random_values=random_values, clock=rollout.runtime.clock.capture(),
                frames={seat: info.sha256 for seat, info in rollout.freeze_due_information().items()},
                executions=deepcopy(rollout.runtime.environment.executions)))
            rollout.runtime.failed = True
        snapshot.restore()
        assert not rollout.runtime.failed
    assert branches[0] == branches[1]
    assert snapshot.native.closed


@pytest.mark.parametrize("owner", [0, 1])
def test_single_ready_native_rejection_never_publishes_episode(rollout, owner, tmp_path):
    recorder = AcceptanceRecorder(tmp_path, match_id=f"rejected-seat-{owner}", attempt_id=1, identity=IDENTITY)
    information = rollout.freeze_due_information()
    actions = {owner: FullAction(11, 0, 1), 1 - owner: FullAction()}
    rollout.runtime.environment.rejected = {owner}
    rollout.commit_simultaneous(actions)
    recorder.record_committed(information, actions, rollout.runtime.clock)
    rollout.advance()
    rollout.advance()
    with pytest.raises(ValueError, match="quarantines"):
        recorder.reconcile(rollout.runtime.clock)
    recorder.quarantine("single-ready rejection diagnostic")
    quarantined = json.loads((recorder.directory / "quarantine.json").read_text())
    assert quarantined["published"] is False and quarantined["training_admissible"] is False
    assert not (tmp_path / "completed").exists()


@pytest.mark.parametrize("actions", [
    {0: FullAction(), 1: FullAction()},
    {0: FullAction(11, 0, 1), 1: FullAction()},
    {0: FullAction(), 1: FullAction(11, 0, 30)},
    {0: FullAction(11, 0, 1), 1: FullAction(11, 0, 2)},
])
def test_no_rejection_public_deployment_protocol_preserves_synchrony(actions):
    runtime = FinalDecisionRuntime(TickEnvironment())
    for boundary in range(4):
        assert runtime.clock.ready == {0, 1}
        runtime.commit_simultaneous(actions, legal=lambda *args: True)
        while not runtime.frozen:
            runtime.advance()
        assert runtime.clock.ready == {0, 1}
        assert not runtime.clock.pending
