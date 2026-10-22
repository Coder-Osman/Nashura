import json
from types import SimpleNamespace

import pytest

import finish_refill_search_validation as finalizer


def setup_attempt(tmp_path, monkeypatch, *, exit_code=0):
    monkeypatch.setattr(finalizer, "ROOT", tmp_path)
    directory = tmp_path/"native-refill-search-test"
    directory.mkdir()
    (directory/"attempt.json").write_text(json.dumps(dict(remote="isolated", exit_code=exit_code)))
    (tmp_path/"native-refill-search-report.json").write_text(json.dumps(dict(remote="isolated")))
    monkeypatch.setattr(finalizer, "completion_summary", lambda report: dict(status="passed"))
    calls = []
    def run(arguments, **kwargs):
        calls.append((arguments[1].split("/")[-1], arguments[2:]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(finalizer.subprocess, "run", run)
    return directory, calls


def test_retry_preserves_failed_finalization_and_native_artifacts(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    previous = tmp_path/(directory.name+"-finalization")
    previous.mkdir()
    status_bytes = b'{"status":"failed","error":"old-verifier"}'
    (previous/"status.json").write_bytes(status_bytes)
    before = (directory/"attempt.json").read_bytes()
    finalizer.finish(directory)
    assert (previous/"status.json").read_bytes() == status_bytes
    assert (directory/"attempt.json").read_bytes() == before
    retries = list(tmp_path.glob(directory.name+"-finalization-retry-*"))
    assert len(retries) == 1
    status = json.loads((retries[0]/"status.json").read_text())
    assert status["status"] == "passed" and status["training_admissible"] is False
    assert calls == [("verify_refill.py", ["--search"]), ("test_refill_evidence.py", []),
        ("test_refill_evidence.py", ["--search"]), ("build_gate_report.py", [])]
    assert (retries[0]/"test_refill_evidence.py.log").is_file()
    assert (retries[0]/"test_refill_evidence.py-search.log").is_file()


def test_failed_native_attempt_stops_before_validation(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch, exit_code=1)
    with pytest.raises(RuntimeError, match="native refill search failed"):
        finalizer.finish(directory)
    assert calls == []


def test_failed_validator_stops_before_gate_update(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    def fail(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(finalizer.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="finalization failed"):
        finalizer.finish(directory)
    assert len(calls) == 1
    assert not (tmp_path/(directory.name+"-finalization")/"completion-summary.json").exists()
