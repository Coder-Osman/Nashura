#!/usr/bin/env python3
"""Observe one isolated loading-key store transaction and restore its value."""

import argparse
import json
import subprocess
import time
from pathlib import Path

import frida


def adb(adb, serial, *args):
    return subprocess.check_output(
        [str(adb), "-s", serial, *args], text=True, stderr=subprocess.STDOUT
    )


def message_payload(record):
    message = record.get("message", {})
    if message.get("type") != "send":
        return None
    return message.get("payload", {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--game-pid", type=int, required=True)
    parser.add_argument("--system-server-pid", type=int, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", default="emulator-5580")
    parser.add_argument("--key", default="LOADING_SCREEN_STATE_LABEL")
    parser.add_argument("--value", default="datatables-init")
    parser.add_argument("--restore-value", default="")
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit("refusing to overwrite output: " + str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)

    source = args.observer.read_text(encoding="utf-8")
    device = frida.get_usb_device(timeout=15)
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

    def on_message(pid, message, data):
        record = {"host_received_ms": int(time.time() * 1000),
                  "target_pid": pid, "message": message}
        if data:
            record["data_bytes"] = len(data)
        records.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)

    def snapshot(label):
        try:
            output = adb(args.adb, args.serial, "shell", "dumpsys", "backup")
            records.append({"host_received_ms": int(time.time() * 1000),
                            "phase": label, "backup_dump": output})
        except subprocess.CalledProcessError as error:
            records.append({"host_received_ms": int(time.time() * 1000),
                            "phase": label, "backup_dump_error": error.output})

    try:
        snapshot("before_attach")
        for pid in (args.system_server_pid, args.game_pid):
            session = device.attach(pid)
            script = session.create_script(source)
            script.on("message", lambda message, data, pid=pid:
                      on_message(pid, message, data))
            script.load()
            sessions.append(session)
            scripts.append(script)

        deadline = time.time() + 15
        required = {
            args.game_pid: {
                "com.supercell.titan.ApplicationUtilBase.storeKeyValue",
                "c.a.n",
                "android.app.SharedPreferencesImpl$EditorImpl.commitToMemory",
                "android.app.SharedPreferencesImpl$EditorImpl.commit",
                "android.app.SharedPreferencesImpl.enqueueDiskWrite",
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
            for record in records:
                payload = message_payload(record)
                if not payload:
                    continue
                if payload.get("event") == "observer_ready":
                    ready_by_pid[record["target_pid"]] = payload.get("details", {})
                if payload.get("event") == "hook_installed":
                    details = payload.get("details", {})
                    name = details.get("className") + "." + details.get("methodName")
                    hooks_by_pid.setdefault(record["target_pid"], set()).add(name)
            if (len(ready_by_pid) == 2 and all(
                    required[pid].issubset(hooks_by_pid.get(pid, set()))
                    for pid in required)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("observers did not become ready with commit hooks")

        records.append({"host_received_ms": int(time.time() * 1000),
                        "phase": "identity", "app": ready_by_pid[args.game_pid],
                        "system_server": ready_by_pid[args.system_server_pid]})
        initial = scripts[1].exports_sync.readkey(args.key)
        records.append({"host_received_ms": int(time.time() * 1000),
                        "phase": "read_initial", "result": initial})
        if args.observe_only:
            records.append({"host_received_ms": int(time.time() * 1000),
                            "phase": "observe_only", "side_effects": False})
            return
        snapshot("before_target_store")

        target_result = scripts[1].exports_sync.triggerstore(args.key, args.value)
        records.append({"host_received_ms": int(time.time() * 1000),
                        "phase": "target_store_return", "result": target_result})
        time.sleep(5)
        snapshot("after_target_store")
        target_read = scripts[1].exports_sync.readkey(args.key)
        records.append({"host_received_ms": int(time.time() * 1000),
                        "phase": "read_after_target_store", "result": target_read})

        restore_result = scripts[1].exports_sync.triggerstore(args.key, args.restore_value)
        records.append({"host_received_ms": int(time.time() * 1000),
                        "phase": "restore_store_return", "result": restore_result})
        time.sleep(5)
        snapshot("after_restore_store")
        restored = scripts[1].exports_sync.readkey(args.key)
        records.append({"host_received_ms": int(time.time() * 1000),
                        "phase": "read_after_restore", "result": restored})
    finally:
        for session in reversed(sessions):
            try:
                session.detach()
            except Exception as error:
                records.append({"host_received_ms": int(time.time() * 1000),
                                "detach_error": str(error)})
        args.output.write_text(
            "\n".join(json.dumps(item, sort_keys=True) for item in records) + "\n",
            encoding="utf-8")


if __name__ == "__main__":
    main()
