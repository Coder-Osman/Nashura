import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import backup_fixture_audit as audit
import run_backup_fixture as runner


@pytest.mark.parametrize("scenario", ["platform_nested", "exception", "first_duplicate",
                                     "unrelated", "observer_failure", "interleaved"])
def test_original_wrappers_preserve_calls_and_scope(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for mock Java boundary tests")
    directory = Path(__file__).parent
    result = subprocess.run([node, str(directory / "backup_fixture_scope.cjs"),
                             str(directory / "backup_fixture_observer.js"), scenario],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MOCK_FIXTURE_PASS " + scenario


def observation():
    return {"schema": runner.SCHEMA, "pid": 522, "installed": True, "events": [],
            "dropped": 0, "observationErrors": 0, "activeScopes": 0, "entries": 0,
            "platformScopes": 0, "catalogReadyPass": False}


def readiness():
    return {"schema": runner.SCHEMA, "pid": 522, "installed": True,
            "hooks": sorted(runner.HOOKS), "targetPackage": runner.PACKAGE}


def reviewed_record():
    events = []

    def event(name, scope=None, **details):
        if scope is not None:
            details["scope"] = scope
        events.append({"event": name, "thread": 1, "timeMs": len(events), "details": details})

    schedule = "com.android.server.backup.KeyValueBackupJob.schedule(int)"
    event("platform_scope", "platform-1", origin="platform_schedule_without_fixture_scope")
    event("callee_enter", "platform-1", method=schedule, state={"scheduled": False})
    event("job_submit", "platform-1", id=1, minLatency=20, maxDelay=86400000, charging=True)
    event("job_submit_return", "platform-1", result=1)
    event("callee_return", "platform-1", method=schedule, state={"scheduled": True})
    for index in range(2):
        scope = str(index + 1)
        event("binder_request", packageName=runner.PACKAGE, callingUid=10001)
        event("entry", scope, targetsNull=False, targetMember=True, state={"targetPending": bool(index)})
        event("queue_put_return", scope, previousNull=index == 0)
        if index == 0:
            methods = ["service.writeToJournalLocked(java.lang.String)",
                       "com.android.server.backup.DataChangedJournal.addPackage(java.lang.String)"]
            for method in methods:
                event("callee_enter", scope, method=method, state={})
            for method in reversed(methods):
                event("callee_return", scope, method=method, state={})
        event("callee_enter", scope, method=schedule, state={"scheduled": True})
        event("callee_return", scope, method=schedule, state={"scheduled": True})
        event("return", scope, state={"targetPending": True})
    identity = {"pid": 522, "cmdline": "system_server", "boot_id": "fixture", "starttime": 1}
    return {"schema": "nashura.backup-fixture-run.v1", "status": "RUNTIME_OBSERVED_REQUIRES_REVIEW",
            "avd": "nashura-e2-backup-contract-api30", "requests_issued": 2,
            "catalog_ready_pass": False, "identity_before": identity,
            "identity_after": identity.copy(), "readiness": readiness(),
            "commands": [{"exit_code": 0}],
            "observation": {**observation(), "entries": 2, "events": events, "platformScopes": 1}}


def test_audit_distinguishes_platform_submission_and_application_request(tmp_path):
    record = reviewed_record()
    result = audit.review(record)
    assert result["branches"] == ["first_enqueue_journal", "duplicate_no_journal"]
    assert len(result["platform_accepted_jobs"]) == 1
    assert result["job_delivery"] == "UNKNOWN"
    assert result["linux_backup_binding"] == "UNRESOLVED"
    assert result["catalog_ready_pass"] is False
    source = tmp_path / "run.json"
    source.write_text(json.dumps(record))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "audit.json"
    audit.audit(source, digest, output)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        audit.audit(source, digest, output)
    with pytest.raises(ValueError, match="SHA"):
        audit.audit(source, "0" * 64, tmp_path / "wrong.json")


@pytest.mark.parametrize("damage", ["failure", "identity", "hooks", "dropped", "errors",
    "active", "command", "uid", "thread", "journal", "pending", "job_result",
    "job_return", "origin", "missing_job", "exception", "entry_count", "order", "nested",
    "platform_count", "platform_nested", "platform_duplicate_submit"])
def test_audit_rejects_incomplete_or_misattributed_evidence(damage):
    record = reviewed_record()
    observed = record["observation"]
    events = observed["events"]
    if damage == "failure": record["status"] = "FAILED"
    elif damage == "identity": record["identity_after"]["pid"] += 1
    elif damage == "hooks": record["readiness"]["hooks"].pop()
    elif damage in {"dropped", "active", "errors"}:
        observed[{"dropped": "dropped", "active": "activeScopes", "errors": "observationErrors"}[damage]] = 1
    elif damage == "command": record["commands"][0]["exit_code"] = None
    elif damage == "uid": next(event for event in events if event["event"] == "binder_request")["details"]["callingUid"] = None
    elif damage == "thread": events[-1]["thread"] = 2
    elif damage == "journal": events[:] = [event for event in events if ".DataChangedJournal.addPackage(" not in event["details"].get("method", "")]
    elif damage == "pending": next(event for event in events if event["event"] == "entry")["details"]["state"]["targetPending"] = True
    elif damage == "job_result": events[3]["details"]["result"] = 0
    elif damage == "job_return": events.pop(3)
    elif damage == "origin": events[0]["details"]["origin"] = "assumed_fixture_request"
    elif damage == "missing_job": events[:] = events[5:]
    elif damage == "exception": events[-1]["event"] = "throw"
    elif damage == "entry_count": observed["entries"] = 3
    elif damage == "order": events[2], events[3] = events[3], events[2]
    elif damage == "nested": events[-2]["details"]["method"] = "wrong"
    elif damage == "platform_count": observed["platformScopes"] += 1
    elif damage == "platform_nested": events[4]["details"]["method"] = "wrong"
    elif damage == "platform_duplicate_submit": events.insert(3, copy.deepcopy(events[2]))
    with pytest.raises(ValueError):
        audit.review(record)


def test_transient_snapshot_not_confused_with_final_count():
    value = observation()
    value.update(entries=1, activeScopes=1)
    runner.validate(value, 522)
    value["activeScopes"] = 0
    with pytest.raises(ValueError, match="entry count"):
        runner.validate(value, 522)


def mocked_run(tmp_path, monkeypatch, damage=None):
    source, apk = tmp_path / "script.js", tmp_path / "fixture.apk"
    source.write_text("fixture script")
    apk.write_bytes(b"fixture apk")
    observed = observation()
    header = readiness()
    commands = []
    detached = []
    if damage == "hook": header["hooks"].pop()

    def run_command(arguments, **kwargs):
        operation = arguments[3:]
        commands.append(operation)
        output = b""
        if operation == ["emu", "avd", "name"]:
            output = b"wrong\n" if damage == "avd" else b"nashura-e2-backup-contract-api30\nOK\n"
        elif operation == ["shell", "getprop", "ro.build.fingerprint"]: output = b"fixture fingerprint"
        elif operation[:3] == ["shell", "pm", "list"]: output = ("package:" + runner.PACKAGE).encode()
        elif operation[:3] == ["shell", "pm", "path"]: output = b"package:/data/app/fixture/base.apk"
        elif operation[:2] == ["exec-out", "cat"]: output = b"wrong" if damage == "apk" else apk.read_bytes()
        elif operation[:3] == ["shell", "am", "broadcast"]:
            if damage == "timeout": raise subprocess.TimeoutExpired(arguments, 45, output=b"partial")
            observed["events"].extend([{"event": "binder_request"}, {"event": "entry"}])
            observed["entries"] += 1
            output = b'Broadcast completed: result=0, data="REQUEST_RETURNED"'
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")

    script = SimpleNamespace(load=lambda: None, exports_sync=SimpleNamespace(
        install=lambda: header, snapshot=lambda: copy.deepcopy(observed)))
    session = SimpleNamespace(create_script=lambda text: script, detach=lambda: detached.append(True))
    device = SimpleNamespace(enumerate_processes=lambda: [SimpleNamespace(pid=522, name="system_server")], attach=lambda pid: session)
    frida = SimpleNamespace(get_device=lambda *args, **kwargs: device)
    monkeypatch.setattr(runner.subprocess, "run", run_command)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(runner, "identity", lambda *args: {"pid": 522, "starttime": 1})
    parameters = (Path("adb"), "emulator-fixture", 522, source,
                  hashlib.sha256(source.read_bytes()).hexdigest(), apk,
                  hashlib.sha256(apk.read_bytes()).hexdigest(), tmp_path / "out", frida)
    return parameters, commands, detached


def test_runner_reuse_verifies_installed_sha_and_exactly_two_requests(tmp_path, monkeypatch):
    parameters, commands, detached = mocked_run(tmp_path, monkeypatch)
    result = runner.run(*parameters, reuse=True)
    assert result["requests_issued"] == 2
    assert sum(operation[:3] == ["shell", "am", "broadcast"] for operation in commands) == 2
    assert not any("install" in operation or "bmgr" in operation for operation in commands)
    assert result["catalog_ready_pass"] is False
    assert detached == [True]
    assert (tmp_path / "out/result.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        runner.run(*parameters, reuse=True)


@pytest.mark.parametrize("damage", ["avd", "hook", "apk", "timeout", "reuse"])
def test_runner_retains_failure_without_retry(tmp_path, monkeypatch, damage):
    parameters, commands, detached = mocked_run(tmp_path, monkeypatch, damage)
    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        runner.run(*parameters, reuse=damage != "reuse")
    record = json.loads((tmp_path / "out/result.json").read_text())
    assert record["status"] == "FAILED"
    assert record["error"]
    assert record["requests_issued"] <= 1
    if damage == "timeout":
        assert record["commands"][-1]["exit_code"] is None
        assert record["commands"][-1]["timeout_seconds"] == 45
    if damage != "avd": assert detached == [True]
