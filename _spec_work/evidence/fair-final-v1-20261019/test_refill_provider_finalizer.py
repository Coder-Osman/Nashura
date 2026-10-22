import json
from types import SimpleNamespace

import pytest

import finish_refill_provider_validation as finalizer


def setup_attempt(tmp_path, monkeypatch, *, exit_code=0):
    monkeypatch.setattr(finalizer, "ROOT", tmp_path)
    directory = tmp_path/"native-refill-provider-test"
    directory.mkdir()
    payload = dict(remote="isolated", source_sha256={"module": "frozen"}, exit_code=exit_code)
    (directory/"attempt.json").write_text(json.dumps(payload))
    (tmp_path/"native-refill-provider-report.json").write_text(json.dumps(dict(remote="isolated")))
    monkeypatch.setattr(finalizer, "completion_summary", lambda report: dict(status="passed", remote=report["remote"]))
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments[1].split("/")[-1])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(finalizer.subprocess, "run", run)
    return directory, calls


def test_successful_finalizer_preserves_native_and_failed_finalization(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    previous = tmp_path/(directory.name+"-finalization")
    previous.mkdir()
    previous_status = b'{"status":"failed","error":"prior-verifier"}'
    (previous/"status.json").write_bytes(previous_status)
    before = {path.name:path.read_bytes() for path in directory.iterdir()}
    finalizer.finish(directory)
    assert calls == ["verify_refill_provider.py", "test_refill_provider_evidence.py", "build_gate_report.py"]
    assert {path.name:path.read_bytes() for path in directory.iterdir()} == before
    assert (previous/"status.json").read_bytes() == previous_status
    retry, = tmp_path.glob(directory.name+"-finalization-retry-*")
    status = json.loads((retry/"status.json").read_text())
    assert status["status"] == "passed" and status["training_admissible"] is False


def test_native_failure_stops_before_validation(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch, exit_code=1)
    with pytest.raises(RuntimeError, match="native refill provider failed"):
        finalizer.finish(directory)
    assert calls == []


@pytest.mark.parametrize("failed", ["verify_refill_provider.py", "test_refill_provider_evidence.py", "build_gate_report.py"])
def test_validator_failure_stops_current_stage(tmp_path, monkeypatch, failed):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    def run(arguments, **kwargs):
        script = arguments[1].split("/")[-1]
        calls.append(script)
        return SimpleNamespace(returncode=int(script == failed))
    monkeypatch.setattr(finalizer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="finalization failed"):
        finalizer.finish(directory)
    assert calls[-1] == failed
    status = json.loads((tmp_path/(directory.name+"-finalization")/"status.json").read_text())
    assert status["status"] == "failed"
    assert not (tmp_path/(directory.name+"-finalization")/"completion-summary.json").exists()


def test_foreign_report_times_out_without_gate_update(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    (tmp_path/"native-refill-provider-report.json").write_text(json.dumps(dict(remote="foreign")))
    ticks = iter((0, 0, 2, 3))
    monkeypatch.setattr(finalizer.time, "monotonic", lambda:next(ticks))
    monkeypatch.setattr(finalizer.time, "sleep", lambda seconds:None)
    with pytest.raises(TimeoutError, match="deadline"):
        finalizer.finish(directory, timeout_seconds=1)
    assert calls == []


def test_changed_attempt_identity_stops_before_validation(tmp_path, monkeypatch):
    directory, calls = setup_attempt(tmp_path, monkeypatch, exit_code=None)
    payload = json.loads((directory/"attempt.json").read_text())
    payload.pop("exit_code")
    (directory/"attempt.json").write_text(json.dumps(payload))
    def replace_attempt(seconds):
        payload["source_sha256"] = {"module": "changed"}
        (directory/"attempt.json").write_text(json.dumps(payload))
    monkeypatch.setattr(finalizer.time, "sleep", replace_attempt)
    with pytest.raises(ValueError, match="identity changed"):
        finalizer.finish(directory)
    assert calls == []


@pytest.mark.parametrize("timeout", [0, -1, 7201])
def test_unbounded_timeout_rejected(tmp_path, monkeypatch, timeout):
    directory, calls = setup_attempt(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="bounded timeout"):
        finalizer.finish(directory, timeout_seconds=timeout)
    assert calls == []
