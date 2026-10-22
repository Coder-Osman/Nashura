import json
from types import SimpleNamespace

import pytest

import finish_local_search_validation as finalizer


def setup_attempt(tmp_path, monkeypatch, *, exit_code=0):
    monkeypatch.setattr(finalizer, "ROOT", tmp_path)
    directory = tmp_path/"native-local-search-test"
    directory.mkdir()
    payload = dict(remote="isolated-attempt", exit_code=exit_code)
    (directory/"attempt.json").write_text(json.dumps(payload))
    (tmp_path/"native-local-search-report.json").write_text(json.dumps(dict(remote="isolated-attempt")))
    monkeypatch.setattr(finalizer, "completion_summary", lambda report: dict(status="passed", remote=report["remote"]))
    calls = []
    monkeypatch.setattr(finalizer.subprocess, "run", lambda argv, **kwargs:
        (calls.append(argv[-1].split("/")[-1]) or SimpleNamespace(returncode=0)))
    return directory, calls


def test_successful_finalizer_keeps_mutable_state_outside_evidence(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    before = {path.name:path.read_bytes() for path in directory.iterdir()}
    finalizer.finish(directory)
    assert calls == ["verify_local_search.py", "test_local_search_evidence.py", "build_gate_report.py"]
    assert {path.name:path.read_bytes() for path in directory.iterdir()} == before
    status = json.loads((tmp_path/(directory.name+"-finalization")/"status.json").read_text())
    assert status["status"] == "passed" and status["training_admissible"] is False


def test_failed_native_attempt_never_runs_validators(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch, exit_code=1)
    with pytest.raises(RuntimeError, match="native local search failed"):
        finalizer.finish(directory)
    assert calls == []
    status = json.loads((tmp_path/(directory.name+"-finalization")/"status.json").read_text())
    assert status["status"] == "failed"


def test_failed_evidence_validation_never_updates_gate(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    def fail(argv, **kwargs):
        calls.append(argv[-1].split("/")[-1])
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(finalizer.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="finalization failed"):
        finalizer.finish(directory)
    assert calls == ["verify_local_search.py"]
    assert not (tmp_path/(directory.name+"-finalization")/"completion-summary.json").exists()


def test_foreign_report_is_not_accepted(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    (tmp_path/"native-local-search-report.json").write_text(json.dumps(dict(remote="other-attempt")))
    ticks = iter((0, 0, 2, 3))
    monkeypatch.setattr(finalizer.time, "monotonic", lambda:next(ticks))
    monkeypatch.setattr(finalizer.time, "sleep", lambda seconds:None)
    with pytest.raises(TimeoutError, match="deadline"):
        finalizer.finish(directory, timeout_seconds=1)
    assert calls == []
