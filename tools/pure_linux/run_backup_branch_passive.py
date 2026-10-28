"""Bound passive observation of natural Backup requests in the isolated AVD."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


SCHEMA = "nashura.backup-branch-observer.v1"
REQUIRED_HOOKS = {
    "java.util.HashMap.put(java.lang.Object,java.lang.Object)",
    "com.android.server.backup.UserBackupManagerService.dataChangedImpl(java.lang.String,java.util.HashSet)",
    "com.android.server.backup.UserBackupManagerService.writeToJournalLocked(java.lang.String)",
    "com.android.server.backup.DataChangedJournal.addPackage(java.lang.String)",
    "com.android.server.backup.KeyValueBackupJob.schedule(int,android.content.Context,com.android.server.backup.BackupManagerConstants)",
    "com.android.server.backup.KeyValueBackupJob.schedule(int,android.content.Context,long,com.android.server.backup.BackupManagerConstants)",
}


def validate_header(value, pid):
    if (value.get("schema") != SCHEMA or value.get("installed") is not True or
            value.get("pid") != pid or value.get("passive") is not True):
        raise ValueError("observer readiness mismatch")


def observation_status(observed, pid):
    validate_header(observed, pid)
    counters = ("dropped", "activeScopes", "observationErrors", "naturalEntries")
    if any(type(observed.get(name)) is not int or observed[name] < 0 for name in counters):
        raise ValueError("invalid observation counters")
    events = observed.get("events")
    if not isinstance(events, list) or any(not isinstance(event, dict) or
            not isinstance(event.get("event"), str) for event in events):
        raise ValueError("invalid observation events")
    if (any(observed[name] for name in counters[:3]) or
            any(event["event"] == "observer_error" for event in events)):
        return "INCOMPLETE_COVERAGE"
    if sum(event["event"] == "entry" for event in events) != observed["naturalEntries"]:
        raise ValueError("entry count mismatch")
    return "NOT_OBSERVED" if observed["naturalEntries"] == 0 else "RUNTIME_OBSERVED_REQUIRES_REVIEW"


def identity(adb, serial, pid):
    def read(path):
        return subprocess.run([str(adb), "-s", serial, "shell", "cat", path],
                              check=True, capture_output=True, timeout=15).stdout

    boot = read("/proc/sys/kernel/random/boot_id").decode().strip()
    command = read("/proc/%d/cmdline" % pid).rstrip(b"\x00\r\n")
    fields = read("/proc/%d/stat" % pid).decode().rsplit(") ", 1)[1].split()
    if command != b"system_server" or not boot or len(fields) < 20:
        raise ValueError("system_server identity mismatch")
    return {"boot_id": boot, "pid": pid, "starttime": int(fields[19]),
            "cmdline": command.decode()}


def run(adb, serial, pid, source_path, expected_sha, seconds, output_path, frida):
    if not 0 < seconds <= 60:
        raise ValueError("passive window must be within 60 seconds")
    source = source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != expected_sha:
        raise ValueError("observer SHA mismatch")
    record = {"schema": "nashura.backup-branch-passive.v1", "serial": serial,
              "script_sha256": expected_sha, "seconds_requested": seconds,
              "status": "FAILED",
              "requests_issued": 0, "stores_issued": 0, "catalog_ready_pass": False}
    with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        session = None
        try:
            before = identity(adb, serial, pid)
            device = frida.get_device(serial, timeout=15)
            names = {process.pid: process.name for process in device.enumerate_processes()}
            if names.get(pid) != "system_server":
                raise ValueError("Frida PID/name mismatch")
            record["identity_before"] = before
            session = device.attach(pid)
            script = session.create_script(source.decode())
            script.load()
            record["readiness"] = script.exports_sync.install()
            validate_header(record["readiness"], pid)
            hooks = record["readiness"].get("hooks", [])
            if not isinstance(hooks, list) or set(hooks) != REQUIRED_HOOKS or len(hooks) != len(REQUIRED_HOOKS):
                raise ValueError("observer hook coverage mismatch")
            if identity(adb, serial, pid) != before:
                raise ValueError("process identity changed during attach")
            start = time.monotonic()
            time.sleep(seconds)
            record["observed_seconds"] = time.monotonic() - start
            record["observation"] = script.exports_sync.snapshot()
            record["identity_after"] = identity(adb, serial, pid)
            if record["identity_after"] != before or source_path.read_bytes() != source:
                raise ValueError("identity changed during observation")
            record["status"] = observation_status(record["observation"], pid)
        except BaseException as error:
            record["error"] = str(error)
            raise
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception as error:
                    record["detach_error"] = str(error)
                    if record["status"] != "FAILED":
                        record["status"] = "INCOMPLETE_COVERAGE"
            json.dump(record, output, indent=2)
            output.write("\n")
    return record


def main():
    import frida

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "script", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--script-sha256", required=True)
    parser.add_argument("--seconds", type=float, default=45)
    args = parser.parse_args()
    record = run(args.adb, args.serial, args.pid, args.script, args.script_sha256,
                 args.seconds, args.output, frida)
    print(json.dumps({"status": record["status"], "catalog_ready_pass": False}))


if __name__ == "__main__":
    main()
