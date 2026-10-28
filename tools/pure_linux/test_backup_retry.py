import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import backup_retry_audit as auditor
import run_backup_retry as runner


def fixture(legacy=False):
    token = "a" * 32
    package = runner.PACKAGE
    identity = {"pid": 470, "starttime": 1100, "cmdline": "system_server", "boot_id": "synthetic"}
    record = {"schema": "nashura.backup-retry-run.v1" if legacy else "nashura.backup-retry-run.v2",
              "file_capture_mode": "shell_T_remote_exit_status", "status": "RUNTIME_OBSERVED_REQUIRES_REVIEW",
              "avd": runner.AVD, "serial": "emulator-5582", "token": token, "catalog_ready_pass": False,
              "stores_issued": 1, "requests_issued": 1, "queue_runs_issued": 3, "game_stores_issued": 0,
              "system_server_hooks": 0, "backupnow_issued": 0, "identity_before": identity,
              "identity_after": identity.copy(), "fingerprint": "synthetic", "snapshots": {},
              "apk_sha256": auditor.digest(b"synthetic APK"), "platform_sha256": auditor.digest(b"synthetic platform")}
    commands, outputs = [], []

    def add(arguments, data=b"", code=0, error=b""):
        commands.append({"arguments": arguments, "exit_code": code})
        outputs.append((data, error))

    apk_path = "/data/app/~~synthetic/fixture/base.apk"
    for args, data in [(["emu", "avd", "name"], (runner.AVD + "\nOK\n").encode()),
                       (["shell", "pm", "list", "packages", "nullsroyale.rel.free"], b""),
                       (["shell", "pm", "list", "packages", package], b""),
                       (["shell", "getprop", "ro.build.fingerprint"], b"synthetic"),
                       (["exec-out", "cat", "/system/framework/services.jar"], b"synthetic platform"),
                       (["shell", "pidof", "system_server"], b"470"),
                       (["install", "/fixture.apk"], b"Success"),
                       (["shell", "pm", "path", package], ("package:" + apk_path).encode()),
                       (["exec-out", "cat", apk_path], b"synthetic APK"),
                       (["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"], b""),
                       (["shell", "bmgr", "enable", "true"], b""), (["shell", "bmgr", "run"], b"")]:
        add(args, data)
    paths = ["/data/user/0/" + package + "/shared_prefs/fixture.xml",
             "/data/user/0/" + package + "/no_backup/fail-once",
             "/data/user/0/com.android.localtransport/files/1/_delta/" + package + "/Zml4dHVyZTpmaXh0dXJl",
             "/data/backup/com.android.localtransport.LocalTransport/" + package]
    xml = ('<map><string name="token">' + token + '</string><string name="payload">retry-v1:' + token + '</string></map>').encode()
    transport, state = b"h" * 16 + xml, b"s" * 72
    retained = "/data/backup/pending/journal1.tmp"
    fresh = "/data/backup/pending/journal2.tmp"
    other = b"\x00\x05other"
    encoded = len(package).to_bytes(2, "big") + package.encode()
    log = b""

    def event(millis, name, thread, suffix=""):
        return ("10-28 15:00:00.%03d 5413 %d I NashuraBackupRetry: %s token=%s%s\n"
                % (millis, thread, name, token, suffix)).encode()

    def worker(millis, thread, kind):
        outcome = ("backup_agent_failure: [" + package + ",result error]" if kind == "fail"
                   else "backup_package: [" + package + ",259]")
        return ("10-28 15:00:00.%03d 470 %d D KeyValueBackupTask: Invoking agent on %s\n"
                "10-28 15:00:00.%03d 470 %d I %s\n"
                "10-28 15:00:00.%03d 470 %d I KeyValueBackupTask: K/V backup pass finished\n"
                % (millis, thread, package, millis + 3, thread, outcome, millis + 4, thread)).encode()

    for index, name in enumerate(("before", "queued", "after_failure", "after_retry")):
        if index == 1:
            add(["shell", "am", "broadcast", "--include-stopped-packages", "-n", package + "/.RetryReceiver",
                 "-a", package + ".STORE_AND_REQUEST", "--es", "token", token], ("RETRY_REQUEST_RETURNED:" + token).encode())
            log += event(1, "STORE_RETURN", 5413) + event(2, "REQUEST_RETURN", 5413)
        elif index > 1:
            add(["shell", "bmgr", "run"])
            if index == 2:
                log += event(4, "AGENT_ENTER", 5430) + event(5, "AGENT_FAIL", 5430) + worker(3, 5450, "fail")
                log += b"NASHURA_CONTROLLED_AGENT_FAILURE\n"
            else:
                log += event(10, "AGENT_ENTER", 5431) + event(11, "AGENT_RETURN", 5431, " new_state_bytes=72") + worker(9, 5451, "success")
        pending = index in (1, 2)
        dump = ("\nNo backups running\nPending key/value backup: %d\n" % pending +
                ("    BackupRequest{pkg=" + package + "}\n" if pending else "") + "Full backup queue:\n").encode()
        indices = {"dump": len(commands)}
        add(["shell", "dumpsys", "backup"], dump)
        indices["journal_listing"] = len(commands)
        journals = {retained: other + (encoded if index == 1 else b"")} if index < 2 else {fresh: other + (encoded if index == 2 else b"")}
        if index == 3:
            journals = {}
        add(["shell", "find", "/data/backup/pending", "-type", "f"], "\n".join(journals).encode())
        indices["journals"] = []
        for path in sorted(journals):
            indices["journals"].append(len(commands))
            add(["exec-out", "cat", path], journals[path])
        files = [[None] * 4, [xml, token.encode(), None, None], [xml, None, None, b""], [xml, None, transport, state]][index]
        indices["files"] = []
        for path, data in zip(paths, files):
            indices["files"].append(len(commands))
            diagnostic = ("cat: " + path + ": No such file or directory\n").encode()
            add((["exec-out", "cat"] if legacy else ["shell", "-T", "cat"]) + [path],
                diagnostic if data is None and legacy else data or b"", 1 if data is None and not legacy else 0,
                diagnostic if data is None and not legacy else b"")
        indices["log"] = len(commands)
        add(["logcat", "-d", "-v", "threadtime", "-b", "all", "-t", "6000"], log)
        record["snapshots"][name] = indices
    add(["exec-out", "cat", apk_path], b"synthetic APK")
    add(["exec-out", "cat", "/system/framework/services.jar"], b"synthetic platform")
    follow_commands = copy.deepcopy(commands[:2]) + [{"arguments": ["shell", "pidof", "system_server"], "exit_code": 0}]
    follow_outputs = copy.deepcopy(outputs[:2]) + [(b"470", b"")]
    for path, data in zip(paths, [xml, None, transport, state]):
        follow_commands.append({"arguments": ["shell", "-T", "cat", path], "exit_code": 1 if data is None else 0})
        follow_outputs.append((data or b"", ("cat: " + path + ": No such file or directory\n").encode() if data is None else b""))
    follow_commands.append({"arguments": ["shell", "dumpsys", "backup"], "exit_code": 0})
    follow_outputs.append((dump, b""))
    return [record, commands, outputs, follow_commands, follow_outputs]


@pytest.mark.parametrize("legacy", [False, True])
def test_synthetic_archive_cannot_promote_linux_gate(legacy):
    report = auditor.review(*fixture(legacy))
    assert report["pending"] == [False, True, True, False]
    assert report["journal_target_counts"] == [0, 1, 1, 0]
    assert report["linux_backup_binding"] == "UNRESOLVED"
    assert report["catalog_ready_pass"] is False
    assert report["journal_delete_method_count"] == "UNKNOWN"
    assert report["task_exception_and_crash_recovery"] == "NOT_RUN"


@pytest.mark.parametrize("damage", ["avd", "serial", "game", "existing", "identity", "counter", "ready", "token",
    "schema", "capture", "fingerprint", "platform", "apk", "duplicate_store", "extra_output", "snapshot", "exit",
    "pending", "target_journal", "append_prefix", "xml", "payload", "transport", "state", "exception", "agent",
    "thread", "worker", "state_bytes", "follow_mutation", "follow_exit", "follow_pending", "follow_payload"])
def test_rejects_incomplete_or_changed_evidence(damage):
    values = fixture()
    record, commands, outputs, follow, follow_outputs = values
    queued, failed, retried = [record["snapshots"][name] for name in ("queued", "after_failure", "after_retry")]

    def replace(index, old, new):
        outputs[index] = (outputs[index][0].replace(old, new), outputs[index][1])

    if damage == "avd": record["avd"] = "other"
    elif damage == "serial": record["serial"] = "emulator-5580"
    elif damage == "game": outputs[1] = (b"package:game", b"")
    elif damage == "existing": outputs[2] = (b"package:fixture", b"")
    elif damage == "identity": record["identity_after"]["starttime"] += 1
    elif damage == "counter": record["stores_issued"] = True
    elif damage == "ready": record["catalog_ready_pass"] = True
    elif damage == "token": record["token"] = "bad"
    elif damage == "schema": record["schema"] = "unknown"
    elif damage == "capture": record["file_capture_mode"] = "exec_out"
    elif damage == "fingerprint": outputs[3] = (b"changed", b"")
    elif damage == "platform": outputs[4] = (b"changed", b"")
    elif damage == "apk": outputs[8] = (b"changed", b"")
    elif damage == "duplicate_store": commands.append(copy.deepcopy(commands[queued["dump"] - 1])); outputs.append((b"", b""))
    elif damage == "extra_output": outputs.append((b"", b""))
    elif damage == "snapshot": queued["dump"] += 1
    elif damage == "exit": commands[0]["exit_code"] = 1
    elif damage == "pending": outputs[failed["dump"]] = outputs[record["snapshots"]["before"]["dump"]]
    elif damage == "target_journal": outputs[failed["journals"][0]] = (b"", b"")
    elif damage == "append_prefix": replace(queued["journals"][0], b"other", b"wrong")
    elif damage == "xml": outputs[queued["files"][0]] = (b"<broken", b"")
    elif damage == "payload": replace(queued["files"][0], b"retry-v1:", b"wrong-v1:")
    elif damage == "transport": outputs[retried["files"][2]] = (b"wrong", b"")
    elif damage == "state": outputs[retried["files"][3]] = (b"", b"")
    elif damage == "exception": replace(failed["log"], b"NASHURA_CONTROLLED_AGENT_FAILURE", b"unknown")
    elif damage == "agent": replace(retried["log"], b"AGENT_FAIL", b"AGENT_RETURN")
    elif damage == "thread": replace(retried["log"], b"5413 5431 I NashuraBackupRetry: AGENT_ENTER", b"5413 5999 I NashuraBackupRetry: AGENT_ENTER")
    elif damage == "worker": replace(retried["log"], b"backup_agent_failure:", b"uncovered:")
    elif damage == "state_bytes": replace(retried["log"], b"new_state_bytes=72", b"new_state_bytes=73")
    elif damage == "follow_mutation": follow[0]["arguments"] = ["shell", "pm", "clear", runner.PACKAGE]
    elif damage == "follow_exit": follow[4]["exit_code"] = 0
    elif damage == "follow_pending": follow_outputs[7] = outputs[queued["dump"]]
    elif damage == "follow_payload": follow_outputs[3] = (b"changed", b"")
    with pytest.raises(ValueError):
        auditor.review(*values)


def archived(directory):
    record, commands, outputs, follow, follow_outputs = fixture()
    inputs = []
    for name, metadata, entries, data in (("run", record, commands, outputs), ("follow", None, follow, follow_outputs)):
        folder = directory / name
        folder.mkdir()
        for index, (entry, pair) in enumerate(zip(entries, data)):
            for extension, content in zip(("stdout", "stderr"), pair):
                (folder / ("command-%03d.%s" % (index, extension))).write_bytes(content)
                entry[extension + "_sha256"] = auditor.digest(content)
        if metadata is not None:
            metadata["commands"] = entries
        path = folder / "result.json"
        path.write_text(json.dumps(metadata if metadata else entries))
        inputs.extend((path, auditor.digest(path.read_bytes())))
    return inputs


def test_archive_sha_binding_and_exclusive_output(tmp_path):
    inputs = archived(tmp_path)
    output = tmp_path / "audit.json"
    assert auditor.audit(*inputs, output)["status"] == "RUNTIME_OBSERVED_LIMITED"
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        auditor.audit(*inputs, output)


@pytest.mark.parametrize("damage", ["json", "stdout", "stderr", "timeout"])
def test_archive_rejects_corruption(tmp_path, damage):
    inputs = archived(tmp_path)
    if damage == "json": inputs[1] = "0" * 64
    elif damage in ("stdout", "stderr"): (tmp_path / "run" / ("command-000." + damage)).write_bytes(b"changed")
    else:
        record = json.loads(inputs[0].read_text())
        record["commands"][0]["exit_code"] = None
        inputs[0].write_text(json.dumps(record))
        inputs[1] = auditor.digest(inputs[0].read_bytes())
    with pytest.raises(ValueError):
        auditor.audit(*inputs, tmp_path / "audit.json")
    assert not (tmp_path / "audit.json").exists()


@pytest.mark.parametrize("damage", ["avd", "game", "existing", "platform", "timeout"])
def test_runner_preserves_preflight_failures_without_write(tmp_path, monkeypatch, damage):
    apk = tmp_path / "fixture.apk"
    apk.write_bytes(b"synthetic APK")
    calls = []

    def command(arguments, **kwargs):
        args = arguments[3:]
        calls.append(args)
        if damage == "timeout":
            raise subprocess.TimeoutExpired(arguments, 45, output=b"partial")
        data = b""
        if args == ["emu", "avd", "name"]: data = ("wrong" if damage == "avd" else runner.AVD).encode()
        elif args == ["shell", "pm", "list", "packages", "nullsroyale.rel.free"] and damage == "game": data = b"package:game"
        elif args == ["shell", "pm", "list", "packages", runner.PACKAGE] and damage == "existing": data = b"package:fixture"
        return SimpleNamespace(stdout=data, stderr=b"", returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", command)
    output = tmp_path / "attempt"
    with pytest.raises(ValueError):
        runner.run(Path("adb"), "emulator-5582", apk, auditor.digest(apk.read_bytes()), "0" * 64, "a" * 32, output)
    record = json.loads((output / "result.json").read_text())
    assert record["status"] == "FAILED"
    assert record["stores_issued"] == record["requests_issued"] == 0
    assert not any("install" in args or "broadcast" in args for args in calls)
    assert record["commands"][0]["exit_code"] is None if damage == "timeout" else True


def test_runner_rejects_protected_serial_before_commands(tmp_path):
    with pytest.raises(ValueError, match="serial"):
        runner.run(Path("adb"), "emulator-5580", tmp_path / "absent", "", "", "a" * 32, tmp_path / "attempt")
    assert not (tmp_path / "attempt").exists()


def test_runner_captures_remote_missing_exit_without_reissuing_store(tmp_path, monkeypatch):
    record, commands, outputs, _, _ = fixture()
    apk = tmp_path / "fixture.apk"
    apk.write_bytes(b"synthetic APK")
    calls = []

    def command(arguments, **kwargs):
        index = len(calls)
        args = arguments[3:]
        expected = commands[index]["arguments"]
        assert args == (["install", str(apk)] if expected[0] == "install" else expected)
        calls.append(args)
        return SimpleNamespace(stdout=outputs[index][0], stderr=outputs[index][1], returncode=commands[index]["exit_code"])

    monkeypatch.setattr(runner.subprocess, "run", command)
    monkeypatch.setattr(runner, "identity", lambda *args: record["identity_before"].copy())
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    result = runner.run(Path("adb"), "emulator-5582", apk, record["apk_sha256"], record["platform_sha256"], record["token"], tmp_path / "run")
    assert result["schema"] == "nashura.backup-retry-run.v2"
    assert result["stores_issued"] == result["requests_issued"] == 1
    assert result["queue_runs_issued"] == 3
    assert len(calls) == len(commands)
    assert any(entry["exit_code"] == 1 and entry["arguments"][:3] == ["shell", "-T", "cat"] for entry in result["commands"])
