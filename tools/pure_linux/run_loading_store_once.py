#!/usr/bin/env python3
"""Observe one isolated loading-key store transaction and restore its value."""

import argparse
import hashlib
import json
import shlex
import subprocess
import threading
import time
from pathlib import Path


def adb(adb, serial, *args):
    return subprocess.check_output(
        [str(adb), "-s", serial, *args], text=True, stderr=subprocess.STDOUT
    )


def message_payload(record):
    message = record.get("message", {})
    if message.get("type") != "send":
        return None
    return message.get("payload", {})


def capture_preference_file(adb_path, serial, remote_path, destination):
    exists = subprocess.run(
        [str(adb_path), "-s", serial, "shell",
         "if [ -f " + shlex.quote(remote_path) + " ]; then echo present; "
         "elif [ -e " + shlex.quote(remote_path) + " ]; then echo nonregular; "
         "else echo absent; fi"],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    if exists == "absent":
        return {"remote_path": remote_path, "state": "ABSENT"}
    if exists != "present":
        raise RuntimeError("preference snapshot is not a regular file")
    content = subprocess.run(
        [str(adb_path), "-s", serial, "exec-out", "cat " + shlex.quote(remote_path)],
        check=True, capture_output=True, timeout=15,
    ).stdout
    with destination.open("xb") as output:
        destination.chmod(0o600)
        output.write(content)
    return {"remote_path": remote_path, "state": "CAPTURED",
            "private_path": str(destination), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def checked_read(exports, key, expected=None):
    result = exports.readkey(key)
    if result.get("key") != key or not isinstance(result.get("value"), str):
        raise RuntimeError("readback has no restorable string value")
    if expected is not None and result["value"] != expected:
        raise RuntimeError("readback does not match expected value")
    return result


def store_and_restore(exports, key, value, restore_value, record, snapshot, wait):
    initial = checked_read(exports, key)
    record({"phase": "read_initial", "result": initial})
    original = initial["value"]
    if restore_value is not None and restore_value != original:
        raise RuntimeError("restore value differs from observed initial value")
    if value == original:
        raise RuntimeError("test value equals initial value")
    snapshot("before_target_store")
    try:
        record({"phase": "target_store_attempt"})
        result = exports.triggerstore(key, value)
        record({"phase": "target_store_return", "result": result})
        wait(5)
        snapshot("after_target_store")
        result = checked_read(exports, key, value)
        record({"phase": "read_after_target_store", "result": result})
    except BaseException as error:
        record({"phase": "target_store_error", "error": str(error)})
        raise
    finally:
        try:
            result = exports.triggerstore(key, original)
            wait(5)
            restored = checked_read(exports, key, original)
            record({"phase": "restore_store_return", "result": result})
            record({"phase": "read_after_restore", "result": restored})
            snapshot("after_restore_store")
        except BaseException as error:
            record({"phase": "restore_failed", "error": str(error),
                    "manual_recovery_required": True})
            raise


def main():
    import frida

    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--game-pid", type=int, required=True)
    parser.add_argument("--system-server-pid", type=int, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", default="emulator-5580")
    parser.add_argument("--key", default="LOADING_SCREEN_STATE_LABEL")
    parser.add_argument("--value", default="datatables-init")
    parser.add_argument("--restore-value")
    parser.add_argument("--preference-file", action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--observe-only", action="store_true")
    mode.add_argument("--execute-store", action="store_true")
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit("refusing to overwrite output: " + str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)

    source = args.observer.read_text(encoding="utf-8")
    device = frida.get_device(args.serial, timeout=15)
    processes = {process.pid: process.name for process in device.enumerate_processes()}
    if processes.get(args.system_server_pid) != "system_server":
        raise SystemExit("system_server PID/name mismatch: %r" %
                         processes.get(args.system_server_pid))
    expected = {
        args.system_server_pid: "system_server",
        args.game_pid: "nullsroyale.rel.free",
    }
    for pid, expected_cmdline in expected.items():
        cmdline = adb(args.adb, args.serial, "shell", "cat",
                      "/proc/%d/cmdline" % pid).replace("\x00", "").strip()
        if cmdline != expected_cmdline:
            raise SystemExit("/proc identity mismatch for %d: %r" % (pid, cmdline))
    if args.game_pid not in processes:
        raise SystemExit("game PID absent from Frida process list")

    records = []
    sessions = []
    scripts = []
    output = args.output.open("x", encoding="utf-8")
    record_lock = threading.RLock()

    def record(item):
        with record_lock:
            item = {"host_received_ms": int(time.time() * 1000), **item}
            records.append(item)
            output.write(json.dumps(item, sort_keys=True) + "\n")
            output.flush()

    def on_message(pid, message, data):
        item = {"target_pid": pid, "message": message}
        if data:
            item["data_bytes"] = len(data)
        record(item)

    def snapshot(label):
        try:
            backup_dump = adb(args.adb, args.serial, "shell", "dumpsys", "backup")
            record({"phase": label, "backup_dump": backup_dump})
            for index, remote_path in enumerate(args.preference_file):
                destination = args.output.parent / (label + "-preference-%d.xml" % index)
                result = capture_preference_file(args.adb, args.serial, remote_path, destination)
                record({"phase": label, "preference_file": result})
        except subprocess.CalledProcessError as error:
            record({"phase": label, "backup_dump_error": error.output})
            raise

    try:
        snapshot("before_attach")
        for pid in (args.system_server_pid, args.game_pid):
            session = device.attach(pid)
            sessions.append(session)
            script = session.create_script(source)
            script.on("message", lambda message, data, pid=pid:
                      on_message(pid, message, data))
            script.load()
            scripts.append(script)

        deadline = time.time() + 15
        required = {
            args.game_pid: {
                "com.supercell.titan.ApplicationUtilBase.storeKeyValue",
                "com.supercell.titan.ApplicationUtilBase.getKeyValue",
                "c.a.n",
                "android.app.SharedPreferencesImpl$EditorImpl.commitToMemory",
                "android.app.SharedPreferencesImpl$EditorImpl.commit",
                "android.app.SharedPreferencesImpl.enqueueDiskWrite",
                "android.app.SharedPreferencesImpl.writeToFile",
            },
            args.system_server_pid: {
                "com.android.server.backup.UserBackupManagerService.dataChangedImpl",
                "com.android.server.backup.UserBackupManagerService.writeToJournalLocked",
                "com.android.server.backup.KeyValueBackupJob.schedule",
            },
        }
        while time.time() < deadline:
            ready_by_pid = {}
            hooks_by_pid = {}
            with record_lock:
                observed = list(records)
            for item in observed:
                payload = message_payload(item)
                if not payload:
                    continue
                if payload.get("event") == "observer_ready":
                    ready_by_pid[item["target_pid"]] = payload.get("details", {})
                if payload.get("event") == "hook_installed":
                    details = payload.get("details", {})
                    name = details.get("className") + "." + details.get("methodName")
                    hooks_by_pid.setdefault(item["target_pid"], set()).add(name)
            if (len(ready_by_pid) == 2 and all(
                    required[pid].issubset(hooks_by_pid.get(pid, set()))
                    for pid in required)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("observers did not become ready with commit hooks")

        record({"phase": "identity", "app": ready_by_pid[args.game_pid],
                "system_server": ready_by_pid[args.system_server_pid]})
        if not args.execute_store:
            initial = checked_read(scripts[1].exports_sync, args.key)
            record({"phase": "read_initial", "result": initial})
            record({"phase": "observe_only", "store_invoked": False})
            return
        store_and_restore(scripts[1].exports_sync, args.key, args.value,
                          args.restore_value, record, snapshot, time.sleep)
    except BaseException as error:
        record({"phase": "experiment_error", "error": str(error)})
        raise
    finally:
        for session in reversed(sessions):
            try:
                session.detach()
            except Exception as error:
                record({"detach_error": str(error)})
        output.close()


if __name__ == "__main__":
    main()
