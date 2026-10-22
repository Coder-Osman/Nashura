import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

import formal_batch
import formal_contract
import formal_host_worker


def test_formal_allocations_are_complete_and_independent():
    teachers = formal_contract.teacher_jobs(Path("."), 1)
    ppo = formal_contract.ppo_jobs(Path("."), 1)
    assert len(teachers) == 216
    assert len(ppo) == 108
    assert Counter(job["host"] for job in teachers) == {
        "37": 48, "34": 48, "35": 48, "39": 36, "41": 36,
    }
    assert Counter(job["opponent_kind"] for job in ppo) == {
        "self_play": 72, "fixed_1250": 12, "recent": 24,
    }
    assert not {job["seed"] for job in teachers} & {job["seed"] for job in ppo}
    assert len({job["episode_id"] for job in teachers + ppo}) == 324


def test_concurrency_caps_match_expanded_rollout_capacity():
    assert formal_contract.CONCURRENCY == {"34": 2, "35": 12, "37": 12, "39": 8, "41": 8}


def test_ppo_validation_accepts_nonterminal_decisions_before_terminal(monkeypatch, tmp_path):
    destination = tmp_path / "episode"
    destination.mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "start_checkpoint_sha256": "parent", "opponent_checkpoints": {},
    }))
    (destination / "part-00000.json").write_text("{}")
    (destination / "part-00000.crev.zst").write_bytes(b"data")
    (destination / "episode.json").write_text(json.dumps({
        "passed": True, "opponent_kind": "self_play", "seed": 16756, "port": 29920,
        "checkpoint_sha256": "parent", "opponent_sha256": "parent",
        "episode": {"ended": True, "truncated": False},
    }))
    monkeypatch.setattr(formal_host_worker, "ROOT", tmp_path)
    from native_runner.training import event_cache
    monkeypatch.setattr(event_cache, "load_event_rl_shard", lambda path, verify: SimpleNamespace(
        records=[SimpleNamespace(done=False), SimpleNamespace(done=True)],
        manifest={"contract": {"collection_mode": "on-policy", "search_enabled": False,
                               "opponent_kind": "self_play", "opponent_sha256": "parent"}}))
    passed, evidence = formal_host_worker.validate({
        "destination": "episode", "opponent_kind": "self_play", "seed": 16756, "port": 29920,
    }, "ppo")
    assert passed is True
    assert evidence["rows"] == 2


def test_ppo_launch_waits_for_verified_teacher_cycle(monkeypatch):
    monkeypatch.setattr(formal_batch, "status", lambda *args: {"passed": False})
    monkeypatch.setattr(formal_batch, "prepare_opponents", lambda *args: pytest.fail("premature weight deployment"))
    with pytest.raises(ValueError, match="216 verified teacher episodes"):
        formal_batch.launch("ppo", 1, 1, Path("inventory"))


def test_worker_keeps_resource_reserve_failure_as_a_retryable_state(monkeypatch):
    def low_resources():
        raise RuntimeError("resource reserve failed")

    monkeypatch.setattr(formal_host_worker, "resources", low_resources)
    assert formal_host_worker.available_slots([{"pids": []}], 2) == (0, "resource reserve failed")


def test_worker_restores_attempted_jobs_after_restart(tmp_path):
    status = tmp_path / "worker-status.json"
    status.write_text(json.dumps({"jobs": [
        {"episode_id": "complete", "state": "passed"},
        {"episode_id": "active", "state": "running"},
        {"episode_id": "exited", "state": "failed"},
    ]}))
    assert formal_host_worker.attempted_jobs(status) == {"active", "exited"}
