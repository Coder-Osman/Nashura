import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import backup_delivery_audit as auditor
import run_backup_delivery as runner


def fixture():
    events = []

    def event(kind, call, method, value, parent=None):
        details = {"call": call, "method": method, "value": value}
        if kind == "enter":
            details["parent"] = parent
        events.append({"sequence": len(events), "event": kind, "thread": 522,
                       "monotonicMs": 100 + len(events), "details": details})

    job = {"id": 42, "userId": 0, "component": "fixture", "minLatency": 20}
    event("enter", "1", auditor.CALLBACK, {"id": 42, "userId": 0, "scheduled": True})
    event("enter", "2", auditor.BACKUP, {"pending": True, "scheduled": False}, "1")
    event("enter", "3", auditor.CANCEL, {"userId": 0, "scheduled": False}, "2")
    event("return", "3", auditor.CANCEL, {"userId": 0, "scheduled": False})
    event("return", "2", auditor.BACKUP, {"pending": True, "scheduled": False})
    event("return", "1", auditor.CALLBACK, {"id": 42, "userId": 0, "scheduled": False, "result": False})
    observed = {"schema": runner.SCHEMA, "pid": 522, "installed": True,
                "events": events, "dropped": 0, "observationErrors": 0,
                "activeScopes": 0, "catalogReadyPass": False}
    identity = {"pid": 522, "cmdline": "system_server", "starttime": 1, "boot_id": "fixture"}
    commands = [
        ["shell", "am", "broadcast", "--include-stopped-packages", "-n",
         runner.PACKAGE + "/.ProbeReceiver", "-a", runner.PACKAGE + ".REQUEST"],
        ["shell", "cmd", "jobscheduler", "run", "-f", "--user", "0", "android", "42"],
    ]
    return {"schema": "nashura.backup-delivery-run.v1", "status": "RUNTIME_OBSERVED_REQUIRES_REVIEW",
            "avd": "nashura-e2-backup-contract-api30", "trigger": auditor.TRIGGER,
            "requests_issued": 1, "forced_jobs_issued": 1, "stores_issued": 0,
            "catalog_ready_pass": False, "identity_before": identity,
            "identity_after": identity.copy(), "observation": observed,
            "prelude": {**observed, "events": []}, "experiment_start_sequence": 0,
            "readiness": {"schema": runner.SCHEMA, "pid": 522, "installed": True,
                          "targetPackage": runner.PACKAGE, "hooks": sorted(runner.HOOKS),
                          "workerCoverage": auditor.WORKER_COVERAGE},
            "after_prepare": {"pending": False},
            "queued": {"pending": True, "scheduled": True, "jobs": [job]},
            "after_delivery": {"pending": False, "scheduled": True},
            "commands": [{"arguments": arguments, "exit_code": 0} for arguments in commands]}


def test_callback_audit_preserves_unknowns():
    result = auditor.review(fixture())
    assert result["clear_hook_entries"] == 0
    assert result["clear_direct_call_coverage"] == "UNKNOWN"
    assert result["transport_payload_parity"] == "UNKNOWN"
    assert result["linux_backup_binding"] == "UNRESOLVED"
    assert result["callback_result"] is False
    assert result["catalog_ready_pass"] is False


def test_audit_rejects_unrelated_call_after_callback():
    record = fixture()
    events = record["observation"]["events"]
    for kind in ("enter", "return"):
        details = {"call": "4", "method": auditor.CLEAR, "value": {"userId": 0}}
        if kind == "enter":
            details["parent"] = None
        events.append({"sequence": len(events), "event": kind, "thread": 522,
                       "monotonicMs": 100 + len(events), "details": details})
    with pytest.raises(ValueError, match="nesting mismatch"):
        auditor.review(record)


@pytest.mark.parametrize("damage", ["status", "identity", "missing_identity", "hooks", "worker",
    "drop", "error", "active", "sequence", "time", "parent", "thread", "throw", "return",
    "method", "duplicate_call", "job", "callback_job", "user", "pending", "ready",
    "callback_result", "scheduled", "command", "forced_id", "extra_force", "request",
    "command_order", "prelude", "store", "bool_counter"])
def test_audit_rejects_incomplete_or_misattributed_records(damage):
    record = fixture()
    events = record["observation"]["events"]
    if damage == "status": record["status"] = "FAILED"
    elif damage == "identity": record["identity_after"]["pid"] += 1
    elif damage == "missing_identity": record["identity_before"].pop("boot_id")
    elif damage == "hooks": record["readiness"]["hooks"].pop()
    elif damage == "worker": record["readiness"].pop("workerCoverage")
    elif damage in {"drop", "error", "active"}:
        record["observation"][{"drop": "dropped", "error": "observationErrors", "active": "activeScopes"}[damage]] = 1
    elif damage == "sequence": events[1]["sequence"] += 1
    elif damage == "time": events[1]["monotonicMs"] = 1
    elif damage == "parent": events[1]["details"]["parent"] = None
    elif damage == "thread": events[1]["thread"] = 1
    elif damage == "throw": events[-1]["event"] = "throw"
    elif damage == "return": events.pop()
    elif damage == "method": events[1]["details"]["method"] = "invented"
    elif damage == "duplicate_call": events[1]["details"]["call"] = "1"
    elif damage == "job": record["queued"]["jobs"].append(record["queued"]["jobs"][0])
    elif damage == "callback_job": events[-1]["details"]["value"]["id"] = 43
    elif damage == "user": events[2]["details"]["value"]["userId"] = 1
    elif damage == "pending": record["after_delivery"]["pending"] = True
    elif damage == "ready": record["catalog_ready_pass"] = True
    elif damage == "callback_result": events[-1]["details"]["value"]["result"] = True
    elif damage == "scheduled": events[1]["details"]["value"]["scheduled"] = True
    elif damage == "command": record["commands"][0]["exit_code"] = None
    elif damage == "forced_id": record["commands"][1]["arguments"][-1] = "43"
    elif damage == "extra_force": record["commands"].append(copy.deepcopy(record["commands"][1]))
    elif damage == "request": record["commands"][0]["arguments"][-1] = "other.REQUEST"
    elif damage == "command_order": record["commands"].reverse()
    elif damage == "prelude": record["experiment_start_sequence"] = 1
    elif damage == "store": record["stores_issued"] = 1
    elif damage == "bool_counter": record["requests_issued"] = True
    with pytest.raises(ValueError):
        auditor.review(record)


def archived_fixture(directory):
    record = fixture()
    source = directory / "result.json"
    observer = b"fixture observer"
    (directory / "observer.js").write_bytes(observer)
    record["script_sha256"] = hashlib.sha256(observer).hexdigest()
    for index, command in enumerate(record["commands"]):
        for extension in ("stdout", "stderr"):
            data = b'data="REQUEST_RETURNED"' if index == 0 and extension == "stdout" else b""
            (directory / ("command-%02d.%s" % (index, extension))).write_bytes(data)
            command[extension + "_sha256"] = hashlib.sha256(data).hexdigest()
    source.write_text(json.dumps(record))
    return source, hashlib.sha256(source.read_bytes()).hexdigest()


def test_audit_checks_private_archives_and_exclusive_output(tmp_path):
    source, digest = archived_fixture(tmp_path)
    output = tmp_path / "audit.json"
    assert auditor.audit(source, digest, output)["command_archives_verified"] == 2
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        auditor.audit(source, digest, output)
    with pytest.raises(ValueError, match="run SHA"):
        auditor.audit(source, "0" * 64, tmp_path / "other.json")


@pytest.mark.parametrize("name", ["observer.js", "command-00.stdout", "command-01.stderr"])
def test_corrupted_archives_rejected(tmp_path, name):
    source, digest = archived_fixture(tmp_path)
    (tmp_path / name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA mismatch"):
        auditor.audit(source, digest, tmp_path / "audit.json")
    assert not (tmp_path / "audit.json").exists()


@pytest.mark.parametrize("scenario", ["nested", "exception", "interleaved", "observer_failure", "limit"])
def test_actual_delivery_wrappers_with_mock_java(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for Java observer control flow mock")
    root = Path(__file__).parent
    result = subprocess.run([node, str(root / "backup_delivery_scope.cjs"),
                             str(root / "backup_delivery_observer.js"), scenario],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MOCK_DELIVERY_PASS " + scenario


@pytest.mark.parametrize("damage", ["avd", "game", "apk", "platform", "timeout", "hooks"])
def test_runner_preserves_failure_without_retry(tmp_path, monkeypatch, damage):
    source = tmp_path / "observer.js"
    source.write_text("fixture")
    archived = fixture()
    commands = []
    detached = []

    def run_command(arguments, **kwargs):
        command = arguments[3:]
        commands.append(command)
        output = b""
        if command[:2] == ["emu", "avd"]:
            output = b"other" if damage == "avd" else b"nashura-e2-backup-contract-api30"
        elif command[:3] == ["shell", "pm", "list"]:
            output = b"package:game" if damage == "game" else b""
        elif command[:3] == ["shell", "pm", "path"]:
            output = b"package:/data/app/fixture/base.apk"
        elif command[:2] == ["exec-out", "cat"]:
            platform = command[-1].endswith("services.jar")
            output = b"bad" if damage == ("platform" if platform else "apk") else b"fixture"
        elif command == ["shell", "bmgr", "run"] and damage == "timeout":
            raise subprocess.TimeoutExpired(arguments, 45, output=b"partial")
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")

    header = archived["readiness"]
    if damage == "hooks": header["hooks"].pop()
    script = SimpleNamespace(on=lambda *args: None, load=lambda: None,
        exports_sync=SimpleNamespace(install=lambda: header, inspect=lambda: {"pending": False},
                                     snapshot=lambda: archived["prelude"]))
    session = SimpleNamespace(create_script=lambda text: script, detach=lambda: detached.append(True))
    device = SimpleNamespace(enumerate_processes=lambda: [SimpleNamespace(pid=522, name="system_server")],
                             attach=lambda pid: session)
    monkeypatch.setattr(runner.subprocess, "run", run_command)
    monkeypatch.setattr(runner, "identity", lambda *args: archived["identity_before"])
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    digest = hashlib.sha256(b"fixture").hexdigest()
    output = tmp_path / "run"
    with pytest.raises(ValueError):
        runner.run(Path("adb"), "fixture", 522, source, digest, digest, digest, output,
                   SimpleNamespace(get_device=lambda *args, **kwargs: device))
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["requests_issued"] == 0
    assert result["forced_jobs_issued"] == 0
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "result.json").stat().st_mode & 0o777 == 0o600
    assert len(commands) == len(result["commands"])
    if damage in {"hooks", "timeout"}: assert detached == [True]
    if damage == "timeout":
        assert result["commands"][-1]["exit_code"] is None
        assert (output / ("command-%02d.stdout" % (len(commands) - 1))).read_bytes() == b"partial"
