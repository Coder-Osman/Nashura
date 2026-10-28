import copy
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import backup_payload_audit as auditor
import run_backup_payload as runner


def fixture():
    package = runner.PACKAGE
    token = "a" * 32
    platform, apk = b"synthetic platform", b"synthetic APK"
    apk_path = "/data/app/~~fixture/fixture/base.apk"
    listing = ["shell", "find", "/data/backup/pending", "-type", "f"]
    dump = ["shell", "dumpsys", "backup"]
    first, second = "/data/backup/pending/journal1.tmp", "/data/backup/pending/journal2.tmp"
    identity = {"pid": 470, "starttime": 1131, "cmdline": "system_server", "boot_id": "fixture"}
    record = {"schema": "nashura.backup-payload-run.v1", "status": "RUNTIME_OBSERVED_REQUIRES_REVIEW",
        "avd": runner.AVD, "token": token, "trigger": "owned_payload_then_bmgr_backupnow_nonincremental",
        "catalog_ready_pass": False, "requests_issued": 1, "stores_issued": 1, "backupnow_issued": 1,
        "game_stores_issued": 0, "system_server_hooks": 0, "identity_before": identity,
        "identity_after": identity.copy(), "fingerprint": "fixture fingerprint",
        "apk_sha256": auditor.digest(apk), "platform_sha256": auditor.digest(platform),
        "before_dump_index": 12, "prelude_log_index": 13, "queued_dump_index": 15,
        "backupnow_index": 16, "after_dump_index": 17, "final_log_index": 18, "backup_files_index": 19}
    queued = ("Pending key/value backup: 1\n    BackupRequest{pkg=" + package + "}\nFull backup queue:\n").encode()
    empty = b"Pending key/value backup: 0\nFull backup queue:\n"
    xml = ('<map><string name="token">' + token + '</string><string name="payload">payload-v1:'
           + token + '</string></map>').encode()
    state = b"s" * 72
    paths = ["/data/user/0/com.android.localtransport/files/1/_delta/" + package + "/Zml4dHVyZTpmaXh0dXJl",
             "/data/user/0/" + package + "/shared_prefs/fixture.xml",
             "/data/backup/com.android.localtransport.LocalTransport/" + package]
    data = [b"h" * 16 + xml, xml, state]

    def event(index, kind, thread, suffix=""):
        return ("10-28 15:00:00.%03d 5413 %d I NashuraBackupPayload: %s token=%s pid=5413 tid=%d%s\n"
                % (index, thread, kind, token, thread, suffix)).encode()

    def worker(index, thread, size):
        return ("10-28 15:00:00.%03d 470 %d D KeyValueBackupTask: Invoking agent on %s\n"
                "10-28 15:00:00.%03d 470 %d I backup_package: [%s,%d]\n"
                "10-28 15:00:00.%03d 470 %d I KeyValueBackupTask: K/V backup pass finished\n"
                % (index, thread, package, index + 1, thread, package, size, index + 2, thread)).encode()

    log = (event(1, "STORE_RETURN", 5413) + event(2, "REQUEST_RETURN", 5413) +
           event(3, "AGENT_ENTER", 5431) + event(4, "AGENT_RETURN", 5431, " new_state_bytes=72") +
           worker(3, 5454, 261) + ("LocalTransport: Performing non-incremental backup for " + package + "\n").encode())
    final_log = log + event(7, "AGENT_ENTER", 5430) + event(8, "AGENT_RETURN", 5430, " new_state_bytes=72") + worker(6, 5679, 0)
    request = ["shell", "am", "broadcast", "--include-stopped-packages", "-n", package + "/.PayloadReceiver",
               "-a", package + ".STORE_AND_REQUEST", "--es", "token", token]
    common = [(["emu", "avd", "name"], (runner.AVD + "\nOK\n").encode()),
              (["shell", "pm", "list", "packages", "nullsroyale.rel.free"], b"")]
    run = common + [
        (["shell", "pm", "list", "packages", package], b""),
        (["shell", "getprop", "ro.build.fingerprint"], b"fixture fingerprint"),
        (["exec-out", "cat", "/system/framework/services.jar"], platform),
        (["shell", "pidof", "system_server"], b"470"), (["install", "/fixture.apk"], b"Success"),
        (["shell", "pm", "path", package], ("package:" + apk_path).encode()),
        (["exec-out", "cat", apk_path], apk),
        (["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"], b""),
        (["shell", "bmgr", "enable", "true"], b""), (["shell", "bmgr", "run"], b""),
        (dump, empty), (["logcat", "-d", "-b", "all", "-t", "2000"], b""),
        (request, ("PAYLOAD_REQUEST_RETURNED:" + token).encode()), (dump, queued),
        (["shell", "bmgr", "backupnow", "--non-incremental", package], b"Backup finished with result: Success"),
        (dump, queued), (["logcat", "-d", "-b", "all", "-t", "5000"], log),
        (["shell", "find", "/data/backup", "/data/user_de/0/com.android.localtransport", "-type", "f"], b""),
        (["exec-out", "cat", apk_path], apk), (["exec-out", "cat", "/system/framework/services.jar"], platform)]
    reads = [(["exec-out", "cat", path], content) for path, content in zip(paths, data)]
    encoded = len(package).to_bytes(2, "big") + package.encode()
    drain = common + reads + [(listing, (first + "\n").encode()),
        (["exec-out", "cat", first], encoded), (dump, queued), (["shell", "bmgr", "run"], b""),
        (dump, empty), (["logcat", "-d", "-b", "all", "-t", "3000"], final_log),
        (listing, (second + "\n").encode())] + reads
    follow = {"schema": "nashura.backup-payload-journal-followup.v1", "status": "RUNTIME_OBSERVED_REQUIRES_REVIEW",
              "read_only": True, "catalog_ready_pass": False, "listing_before_index": 6,
              "dump_index": 8, "listing_after_index": 9, "platform_index": 11, "installed_apk_index": 12}
    stat = "470 (system_server) " + " ".join(["S"] + ["0"] * 18 + ["1131"])
    follow_pairs = common + [(["shell", "getprop", "ro.build.fingerprint"], b"fixture fingerprint"),
        (["shell", "pidof", "system_server"], b"470"), (["shell", "cat", "/proc/470/stat"], stat.encode()),
        (["shell", "cat", "/proc/sys/kernel/random/boot_id"], b"fixture"),
        (listing, (second + "\n").encode()), (["exec-out", "cat", second], b""), (dump, empty),
        (listing, (second + "\n").encode()), (["shell", "pm", "path", package], ("package:" + apk_path).encode()),
        (["exec-out", "cat", "/system/framework/services.jar"], platform), (["exec-out", "cat", apk_path], apk)]

    def split(pairs):
        return [{"arguments": args, "exit_code": 0} for args, _ in pairs], [output for _, output in pairs]

    commands, outputs = split(run)
    drain_commands, drain_outputs = split(drain)
    follow_commands, follow_outputs = split(follow_pairs)
    return [record, commands, outputs, drain_commands, drain_outputs, follow, follow_commands, follow_outputs]


def test_success_keeps_native_and_failure_boundaries():
    result = auditor.review(*fixture())
    assert result["direct_backup_left_pending"] is True
    assert result["after_queue_drain_pending"] is False
    assert result["files"]["state"]["bytes"] == 72
    assert result["linux_backup_binding"] == "UNRESOLVED"
    assert result["journal_delete_method_count"] == "UNKNOWN"
    assert result["failure_paths"] == "NOT_RUN"
    assert result["catalog_ready_pass"] is False


@pytest.mark.parametrize("damage", ["avd", "game", "identity", "counter", "ready", "index", "order",
    "duplicate_store", "platform", "apk", "fingerprint", "thread", "token", "agent", "worker",
    "pending", "xml", "transport", "state", "journal", "retained", "follow_mutation", "follow_index",
    "follow_fingerprint", "follow_identity", "follow_requeued"])
def test_rejects_damaged_evidence(damage):
    records = fixture()
    run, commands, outputs, drain_commands, drain_outputs, follow, follow_commands, follow_outputs = records
    if damage == "avd": run["avd"] = "other"
    elif damage == "game": outputs[1] = b"package:game"
    elif damage == "identity": run["identity_after"]["pid"] += 1
    elif damage == "counter": run["stores_issued"] = True
    elif damage == "ready": run["catalog_ready_pass"] = True
    elif damage == "index": run["final_log_index"] = 13
    elif damage == "order": commands[14], commands[16] = commands[16], commands[14]
    elif damage == "duplicate_store": commands.append(copy.deepcopy(commands[14])); outputs.append(outputs[14])
    elif damage == "platform": outputs[21] = b"changed"
    elif damage == "apk": outputs[8] = b"changed"
    elif damage == "fingerprint": outputs[3] = b"changed"
    elif damage in {"thread", "token", "agent", "worker"}:
        previous, replacement = {"thread": (b"tid=5413", b"tid=1"), "token": (b"token=aaaa", b"token=bbbb"),
            "agent": (b"AGENT_RETURN", b"AGENT_THROW"), "worker": (b",261]", b",0]")}[damage]
        outputs[18] = outputs[18].replace(previous, replacement)
    elif damage == "pending": drain_outputs[9] = outputs[15]
    elif damage == "xml": drain_outputs[3] = drain_outputs[-2] = b"<map/>"
    elif damage == "transport": drain_outputs[2] = drain_outputs[-3] = b"bad transport"
    elif damage == "state": drain_outputs[4] = drain_outputs[-1] = b"s"
    elif damage == "journal": drain_outputs[6] = b"\x00\x7fshort"
    elif damage == "retained": drain_outputs[11] = drain_outputs[5]
    elif damage == "follow_mutation": follow_commands[1]["arguments"] = ["shell", "pm", "clear", runner.PACKAGE]
    elif damage == "follow_index": follow["dump_index"] = True
    elif damage == "follow_fingerprint": follow_outputs[2] = b"changed"
    elif damage == "follow_identity": follow_outputs[3] = b"999"
    elif damage == "follow_requeued": follow_outputs[7] = drain_outputs[6]
    with pytest.raises(ValueError):
        auditor.review(*records)


def archived(directory):
    records = fixture()
    paths = []
    for name, prefix, record, commands, outputs in (
            ("run", "command-%02d", records[0], records[1], records[2]),
            ("drain", "%02d", None, records[3], records[4]),
            ("follow", "%02d", records[5], records[6], records[7])):
        folder = directory / name
        folder.mkdir()
        for index, (command, output) in enumerate(zip(commands, outputs)):
            for extension, content in (("stdout", output), ("stderr", b"")):
                (folder / (prefix % index + "." + extension)).write_bytes(content)
                command[extension + "_sha256"] = auditor.digest(content)
        if record is not None:
            record["commands"] = commands
        source = folder / "result.json"
        source.write_text(json.dumps(commands if record is None else record))
        paths.extend((source, auditor.digest(source.read_bytes())))
    return paths


def test_archive_binding_and_exclusive_output(tmp_path):
    inputs = archived(tmp_path)
    output = tmp_path / "audit.json"
    result = auditor.audit(*inputs, output)
    assert result["command_archives_verified"] == 50
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        auditor.audit(*inputs, output)


@pytest.mark.parametrize("damage", ["input_sha", "stdout", "stderr", "exit_code"])
def test_archive_rejects_corruption(tmp_path, damage):
    inputs = archived(tmp_path)
    if damage == "input_sha": inputs[1] = "0" * 64
    elif damage in {"stdout", "stderr"}: (tmp_path / "run" / ("command-00." + damage)).write_bytes(b"tampered")
    else:
        record = json.loads(inputs[0].read_text())
        record["commands"][0]["exit_code"] = 1
        inputs[0].write_text(json.dumps(record))
        inputs[1] = auditor.digest(inputs[0].read_bytes())
    with pytest.raises(ValueError):
        auditor.audit(*inputs, tmp_path / "audit.json")
    assert not (tmp_path / "audit.json").exists()


@pytest.mark.parametrize("damage", ["avd", "game", "existing", "platform", "timeout"])
def test_runner_retains_failed_attempt_without_store_or_retry(tmp_path, monkeypatch, damage):
    apk = tmp_path / "fixture.apk"
    apk.write_bytes(b"fixture")
    calls = []

    def command(arguments, **kwargs):
        args = arguments[3:]
        calls.append(args)
        content = b""
        if args == ["emu", "avd", "name"]: content = ("other" if damage == "avd" else runner.AVD).encode()
        elif args == ["shell", "pm", "list", "packages", "nullsroyale.rel.free"]:
            content = b"package:game" if damage == "game" else b""
        elif args == ["shell", "pm", "list", "packages", runner.PACKAGE]:
            content = b"package:existing" if damage == "existing" else b""
        elif args == ["exec-out", "cat", "/system/framework/services.jar"]:
            if damage == "timeout": raise subprocess.TimeoutExpired(arguments, 45, output=b"partial")
            content = b"changed"
        return SimpleNamespace(returncode=0, stdout=content, stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", command)
    output = tmp_path / "run"
    with pytest.raises(ValueError):
        runner.run(Path("adb"), "fixture", apk, auditor.digest(b"fixture"), auditor.digest(b"platform"), "a" * 32, output)
    record = json.loads((output / "result.json").read_text())
    assert record["status"] == "FAILED"
    assert record["stores_issued"] == 0
    assert len(calls) == len(record["commands"])
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "result.json").stat().st_mode & 0o777 == 0o600
    assert not any(args[:3] == ["shell", "am", "broadcast"] for args in calls)
    if damage == "timeout":
        assert record["commands"][-1]["exit_code"] is None
        assert (output / "command-04.stdout").read_bytes() == b"partial"
