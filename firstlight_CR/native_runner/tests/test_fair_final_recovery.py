from dataclasses import asdict, dataclass
import json
import hashlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_rollout import rollout as rollout_fixture
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.recovery import RecoveryJournal, rng_states
from native_runner.training.fair_search.contracts import semantic_digest


@dataclass
class MatchFixture:
    seed: int = 91


IDENTITY = dict(engine_sha256="a" * 64, checkpoint_sha256="b" * 64,
    source_manifest_sha256="c" * 64, controller="fixture-clock", adapter="fixture-policy")
INPUTS = dict(match_config=asdict(MatchFixture()), reset_options={}, execution_seed=19)


@pytest.fixture
def factory(monkeypatch):
    def create(inputs=INPUTS, episode_id="test-episode"):
        rollout = rollout_fixture.__wrapped__(monkeypatch)
        environment = rollout.runtime.environment
        environment.match_config = MatchFixture(**inputs["match_config"])
        environment.episode_id = episode_id
        environment.native = SimpleNamespace(_request=lambda command: dict(sha256=semantic_digest(
            dict(tick=environment.current_tick, executions=environment.executions))))
        return rollout
    return create


def select(information, generators):
    return {owner: FullAction(11, 0, int(torch.randint(1, 5, (), generator=generators[owner])))
        if owner == 0 else FullAction() for owner in information}


def run_to(journal, target):
    while journal.rollout.runtime.clock.tick < target:
        if journal.rollout.runtime.clock.ready:
            journal.commit(select)
        journal.advance()


def summary(journal):
    return dict(clock=journal.rollout.runtime.clock.capture(), rng=rng_states(journal.rollout),
        fair={str(owner): info.sha256 for owner, info in journal.rollout.freeze_due_information().items()})


@pytest.mark.parametrize("tick", [100, 101, 102, 109, 130])
def test_restart_preserves_pending_boundary_frames_rng_and_lifecycle(tmp_path, factory, tick):
    root = tmp_path / "audit"
    original = RecoveryJournal.create(root, factory(), identity=IDENTITY, private_inputs=INPUTS)
    run_to(original, tick)
    original.checkpoint()
    expected = summary(original)
    manifest_sha = original.manifest_sha
    original.close()
    for attempt in range(2):
        with RecoveryJournal.recover(root, factory, expected_identity=IDENTITY,
                expected_manifest_sha=manifest_sha) as restored:
            assert summary(restored) == expected
            assert restored.rollout.runtime.environment is not original.rollout.runtime.environment
    original.rollout.runtime.failed = False
    with RecoveryJournal.recover(root, factory, expected_identity=IDENTITY,
            expected_manifest_sha=manifest_sha) as restored:
        baseline = original.rollout
        for step in range(20):
            for rollout in (baseline, restored.rollout):
                information = rollout.freeze_due_information()
                if information:
                    rollout.commit_simultaneous(select(information, rollout.generators))
                rollout.advance()
        assert summary(restored) == summary(original)


def test_commit_intent_recovers_crash_before_in_memory_commit(tmp_path, factory, monkeypatch):
    original = RecoveryJournal.create(tmp_path / "audit", factory(), identity=IDENTITY, private_inputs=INPUTS)
    def crash(actions):
        raise RuntimeError("crash after durable intent")
    monkeypatch.setattr(original.rollout, "commit_simultaneous", crash)
    with pytest.raises(RuntimeError, match="durable intent"):
        original.commit(select)
    assert original.rollout.runtime.failed
    expected_rng = rng_states(original.rollout)
    original.close()
    with RecoveryJournal.recover(original.root, factory, expected_identity=IDENTITY,
            expected_manifest_sha=original.manifest_sha) as restored:
        assert not restored.rollout.runtime.clock.ready
        assert restored.rollout.runtime.clock.next_identity == 2
        assert rng_states(restored.rollout) == expected_rng
        restored.advance()


def test_writer_lease_prevents_concurrent_recovery(tmp_path, factory):
    original = RecoveryJournal.create(tmp_path / "audit", factory(), identity=IDENTITY, private_inputs=INPUTS)
    try:
        with pytest.raises(BlockingIOError):
            RecoveryJournal.recover(original.root, factory, expected_identity=IDENTITY,
                expected_manifest_sha=original.manifest_sha)
    finally:
        original.close()


@pytest.mark.parametrize("damage", ["checksum", "truncated", "gap", "manifest", "identity"])
def test_corrupt_logs_rejected_before_starting_engine(tmp_path, factory, damage):
    root = tmp_path / "audit"
    with RecoveryJournal.create(root, factory(), identity=IDENTITY, private_inputs=INPUTS) as original:
        original.commit(select)
        original.advance()
        original.checkpoint()
    identity = dict(IDENTITY)
    if damage == "checksum":
        record = json.loads((root / "00000000.json").read_text())
        record["body"]["operation"]["actions"]["0"]["delay_slot"] += 1
        (root / "00000000.json").write_text(json.dumps(record))
    elif damage == "truncated":
        (root / "00000001.json").write_bytes(b"{")
    elif damage == "gap":
        (root / "00000001.json").rename(root / "00000002.json")
    elif damage == "manifest":
        (root / "manifest.json").write_bytes(b"{}")
    else:
        identity["checkpoint_sha256"] = "d" * 64
    def unexpected(*args):
        pytest.fail("invalid identity must fail before engine creation")
    with pytest.raises(ValueError):
        RecoveryJournal.recover(root, unexpected, expected_identity=identity,
            expected_manifest_sha=original.manifest_sha)


def test_wrong_native_state_fails_closed(tmp_path, factory):
    with RecoveryJournal.create(tmp_path / "audit", factory(), identity=IDENTITY, private_inputs=INPUTS) as original:
        original.commit(select)
    broken = factory()
    broken.runtime.environment.native = SimpleNamespace(_request=lambda command: dict(sha256="wrong"))
    with pytest.raises(ValueError, match="opening proof"):
        RecoveryJournal.recover(original.root, lambda *args: broken, expected_identity=IDENTITY,
            expected_manifest_sha=original.manifest_sha)
    assert broken.runtime.failed


@pytest.mark.parametrize("failure", ["illegal", "partial", "truncated", "rejected"])
def test_unconfirmed_or_invalid_transitions_are_not_silently_recovered(tmp_path, factory, failure):
    with RecoveryJournal.create(tmp_path / "audit", factory(), identity=IDENTITY, private_inputs=INPUTS) as journal:
        if failure in ("illegal", "partial"):
            with pytest.raises(ValueError):
                journal.commit(lambda info, generators: {0: FullAction(999, 0, 1)}
                    if failure == "illegal" else {0: FullAction()})
        elif failure == "truncated":
            journal.rollout.runtime.truncated = True
            with pytest.raises(ValueError, match="truncated"):
                journal.checkpoint()
        else:
            journal.commit(lambda info, generators: {0: FullAction(11, 0, 1), 1: FullAction()})
            journal.rollout.runtime.environment.rejected = {0}
            journal.advance()
            with pytest.raises(ValueError, match="rejected"):
                journal.advance()


def test_preemption_survives_replay(tmp_path, factory):
    def with_events(inputs=INPUTS, episode_id="test-episode"):
        rollout = factory(inputs, episode_id)
        rollout.runtime.environment.events = {101}
        return rollout
    with RecoveryJournal.create(tmp_path / "audit", with_events(), identity=IDENTITY, private_inputs=INPUTS) as journal:
        journal.commit(select)
        journal.advance()
        assert all(entry["reason"] == "preempted" for entry in journal.rollout.runtime.clock.evidence)
        expected = summary(journal)
    with RecoveryJournal.recover(journal.root, with_events, expected_identity=IDENTITY,
            expected_manifest_sha=journal.manifest_sha) as restored:
        assert summary(restored) == expected


def test_single_owner_boundary_preserves_other_owner_pending(tmp_path, factory):
    def private_timeout(inputs=INPUTS, episode_id="test-episode"):
        rollout = factory(inputs, episode_id)
        rollout.runtime.environment.events = set()
        rollout.runtime.environment.action_outcomes = lambda: ()
        return rollout
    with RecoveryJournal.create(tmp_path / "audit", private_timeout(), identity=IDENTITY, private_inputs=INPUTS) as journal:
        journal.commit(lambda info, generators: {0: FullAction(11, 0, 1), 1: FullAction()})
        journal.advance()
        journal.advance()
        assert journal.rollout.runtime.clock.ready == {0}
        assert set(journal.rollout.runtime.clock.pending) == {1}
        expected = summary(journal)
    with RecoveryJournal.recover(journal.root, private_timeout, expected_identity=IDENTITY,
            expected_manifest_sha=journal.manifest_sha) as restored:
        assert summary(restored) == expected


def test_terminal_recovery_keeps_same_decision_count(tmp_path, factory):
    def terminal_factory(inputs=INPUTS, episode_id="test-episode"):
        rollout = factory(inputs, episode_id)
        rollout.runtime.environment.terminal_tick = 110
        return rollout
    with RecoveryJournal.create(tmp_path / "audit", terminal_factory(), identity=IDENTITY, private_inputs=INPUTS) as journal:
        run_to(journal, 110)
        assert journal.rollout.runtime.ended
        expected = summary(journal)
    with RecoveryJournal.recover(journal.root, terminal_factory, expected_identity=IDENTITY,
            expected_manifest_sha=journal.manifest_sha) as restored:
        assert restored.rollout.runtime.ended
        assert summary(restored) == expected


def test_actual_process_exit_after_intent_releases_lease_and_recovers(tmp_path, factory):
    root = tmp_path / "audit"
    script = """
import os, sys, pytest
from native_runner.tests.test_fair_final_recovery import factory, IDENTITY, INPUTS, select
from native_runner.training.fair_final.recovery import RecoveryJournal
with pytest.MonkeyPatch.context() as patch:
    create = factory.__wrapped__(patch)
    journal = RecoveryJournal.create(sys.argv[1], create(), identity=IDENTITY, private_inputs=INPUTS)
    journal.rollout.commit_simultaneous = lambda actions: os._exit(73)
    journal.commit(select)
"""
    child = subprocess.run([sys.executable, "-c", script, str(root)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    manifest_sha = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    with RecoveryJournal.recover(root, factory, expected_identity=IDENTITY,
            expected_manifest_sha=manifest_sha) as restored:
        assert restored.sequence == 1
        assert restored.rollout.runtime.clock.next_identity == 2
        assert set(restored.rollout.runtime.clock.pending) == {0, 1}
        run_to(restored, 120)
        restored.checkpoint()


@pytest.mark.parametrize("failure", ["rejected", "truncated"])
def test_invalid_attempt_stays_quarantined_after_restart(tmp_path, factory, failure):
    with RecoveryJournal.create(tmp_path / "audit", factory(), identity=IDENTITY, private_inputs=INPUTS) as journal:
        journal.commit(lambda info, generators: {0: FullAction(11, 0, 1), 1: FullAction()})
        if failure == "truncated":
            journal.rollout.runtime.truncated = True
            with pytest.raises(ValueError, match="truncated"):
                journal.checkpoint()
        else:
            journal.rollout.runtime.environment.rejected = {0}
            journal.advance()
            with pytest.raises(ValueError, match="rejected"):
                journal.advance()
    assert (journal.root / "quarantine.json").is_file()
    def unexpected(*args):
        pytest.fail("quarantined attempts must not start an engine")
    with pytest.raises(ValueError, match="quarantined"):
        RecoveryJournal.recover(journal.root, unexpected, expected_identity=IDENTITY,
            expected_manifest_sha=journal.manifest_sha)
