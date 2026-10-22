import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import prototype_host_worker as worker
from prototype_contract import jobs, FLEET


@pytest.mark.parametrize("running,memory,limit,expected", [(0, 32, 8, 0), (0, 34, 8, 0),
                                                         (0, 37, 2, 2), (2, 50, 2, 0),
                                                         (23, 60, 8, 0), (4, 39.5, 8, 3)])
def test_admission_reserves_ram_and_limits_collectors(running, memory, limit, expected):
    states = [{"pids": list(range(running))}]
    assert worker.available_slots(states, limit, memory) == expected


def process_for(job, root):
    return ["/project/.venv/bin/python", str(root / "event_puct_acceptance.py"),
            "--port", str(job["port"]), "--device", job["device"], "--seed", str(job["seed"]),
            "--episode-id", job["episode_id"], "--simulations", "32",
            "--output", str(root / job["output"])]


def test_resume_checks_entire_job_identity_instead_of_only_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    job = jobs({"fleet": FLEET})[0]
    arguments = process_for(job, tmp_path)
    assert worker.matches_job(arguments, job)
    for name in ("port", "device", "seed", "episode-id", "output"):
        broken = arguments.copy()
        broken[broken.index("--" + name) + 1] = "other"
        assert worker.matches_job(broken, job) is False
    assert worker.matches_job(["other"], job) is False


def test_start_job_uses_absolute_paths_and_preserves_existing_log(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "owned_alive", lambda *args: True)
    job = jobs({"fleet": FLEET})[0]
    (tmp_path / "formal/prototype").mkdir(parents=True)
    (tmp_path / job["log"]).write_text("previous attempt\n")
    service = SimpleNamespace(load=lambda path: {"ready": True}, request=lambda *args: {
        "ok": True, "coldReady": True, "contentReady": True, "snapshotHandles": 0})
    captured = {}
    def launch(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(pid=321)
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    assert worker.start_job(job, service).pid == 321
    assert captured["cwd"] == tmp_path
    assert Path(captured["command"][1]).is_absolute()
    assert captured["command"][-1] == str(tmp_path / job["output"])
    assert (tmp_path / job["log"]).read_text().startswith("previous attempt\n")
    assert (tmp_path / job["pid"]).read_text() == "321\n"


def test_start_job_rejects_leaked_snapshots_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "owned_alive", lambda *args: True)
    service = SimpleNamespace(load=lambda path: {"ready": True}, request=lambda *args: {
        "ok": True, "coldReady": True, "contentReady": True, "snapshotHandles": 2})
    with pytest.raises(RuntimeError, match="clean and ready"):
        worker.start_job(jobs({"fleet": FLEET})[0], service)
