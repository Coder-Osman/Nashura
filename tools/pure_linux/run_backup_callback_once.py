#!/usr/bin/env python3
"""Attach scoped observers to the isolated API 30 AVD and trigger one callback."""

import argparse
import json
import subprocess
import time
from pathlib import Path

import frida


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--game-pid", type=int, required=True)
    parser.add_argument("--system-server-pid", type=int, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", default="emulator-5580")
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit("refusing to overwrite output: " + str(args.output))
    source = args.observer.read_text(encoding="utf-8")
    device = frida.get_usb_device(timeout=15)
    processes = {process.pid: process.name for process in device.enumerate_processes()}
    if processes.get(args.system_server_pid) != "system_server":
        raise SystemExit("system_server PID/name mismatch: %r" %
                         processes.get(args.system_server_pid))
    for pid, expected_cmdline in ((args.system_server_pid, "system_server"),
                                  (args.game_pid, "nullsroyale.rel.free")):
        cmdline = subprocess.check_output(
            [str(args.adb), "-s", args.serial, "shell", "cat",
             "/proc/%d/cmdline" % pid], text=True).replace("\x00", "").strip()
        if cmdline != expected_cmdline:
            raise SystemExit("/proc identity mismatch for %d: %r" % (pid, cmdline))
    if args.game_pid not in processes:
        raise SystemExit("game PID absent from Frida process list")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    sessions = []
    scripts = []
    records = []

    def on_message(pid, message, data):
        record = {"host_received_ms": int(time.time() * 1000), "target_pid": pid,
                  "message": message}
        if data:
            record["data_bytes"] = len(data)
        records.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)

    try:
        for pid in (args.system_server_pid, args.game_pid):
            session = device.attach(pid)
            script = session.create_script(source)
            script.on("message", lambda message, data, pid=pid: on_message(pid, message, data))
            script.load()
            sessions.append(session)
            scripts.append(script)

        deadline = time.time() + 15
        while time.time() < deadline:
            ready = [record for record in records
                     if record.get("message", {}).get("type") == "send"
                     and record["message"].get("payload", {}).get("event") == "observer_ready"]
            ready_by_pid = {record["target_pid"]: record["message"]["payload"]["details"]
                            for record in ready}
            installed = [record for record in records
                         if record.get("message", {}).get("type") == "send"
                         and record["message"].get("payload", {}).get("event") == "hook_installed"]
            hooks_by_pid = {}
            for record in installed:
                details = record["message"]["payload"].get("details", {})
                hooks_by_pid.setdefault(record["target_pid"], set()).add(
                    details.get("className") + "." + details.get("methodName"))
            required = {
                args.game_pid: {"com.supercell.titan.GameApp.requestBackup"},
                args.system_server_pid: {
                    "com.android.server.backup.UserBackupManagerService.dataChangedImpl",
                    "com.android.server.backup.UserBackupManagerService.writeToJournalLocked",
                    "com.android.server.backup.KeyValueBackupJob.schedule",
                },
            }
            if len(ready_by_pid) == 2 and all(
                    required[pid].issubset(hooks_by_pid.get(pid, set())) for pid in required):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("observers did not become ready in both processes")

        server_identity = ready_by_pid[args.system_server_pid]
        app_identity = ready_by_pid[args.game_pid]
        if server_identity.get("uid") != 1000 or server_identity.get("processName") is not None:
            raise RuntimeError("system_server observer identity mismatch: " + repr(server_identity))
        if app_identity.get("processName") != "nullsroyale.rel.free":
            raise RuntimeError("game observer identity mismatch: " + repr(app_identity))

        result = scripts[1].exports_sync.triggerbackup()
        if result.get("invoked") is not True or result.get("invocation") != "instance":
            raise RuntimeError("unexpected trigger result: " + repr(result))
        time.sleep(5)
    finally:
        for session in reversed(sessions):
            try:
                session.detach()
            except Exception as error:  # preserve cleanup diagnostics in the output
                records.append({"host_received_ms": int(time.time() * 1000),
                                "detach_error": str(error)})
        args.output.write_text("\n".join(json.dumps(item, sort_keys=True) for item in records) +
                               "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
