"""Observe a dedicated local test application's real Backup service requests."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from run_backup_branch_passive import identity


PACKAGE = "org.nashura.backupfixture"
SCHEMA = "nashura.backup-fixture-observer.v1"
HOOKS = {
    "com.android.server.backup.UserBackupManagerService.dataChanged(java.lang.String)",
    "com.android.server.backup.UserBackupManagerService.dataChangedImpl(java.lang.String,java.util.HashSet)",
    "java.util.HashMap.put(java.lang.Object,java.lang.Object)",
    "com.android.server.backup.UserBackupManagerService.writeToJournalLocked(java.lang.String)",
    "com.android.server.backup.DataChangedJournal.addPackage(java.lang.String)",
    "com.android.server.backup.KeyValueBackupJob.schedule(int,android.content.Context,com.android.server.backup.BackupManagerConstants)",
    "com.android.server.backup.KeyValueBackupJob.schedule(int,android.content.Context,long,com.android.server.backup.BackupManagerConstants)",
    "android.app.JobSchedulerImpl.schedule(android.app.job.JobInfo)",
}


def validate(value, pid, readiness=False):
    if (value.get("schema") != SCHEMA or value.get("pid") != pid or
            value.get("installed") is not True):
        raise ValueError("fixture observer identity mismatch")
    if readiness:
        hooks = value.get("hooks")
        if (value.get("targetPackage") != PACKAGE or not isinstance(hooks, list) or
                len(hooks) != len(HOOKS) or set(hooks) != HOOKS):
            raise ValueError("fixture hook coverage mismatch")
        return
    for name in ("dropped", "observationErrors", "activeScopes", "entries"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise ValueError("invalid fixture observation counter")
    if not isinstance(value.get("events"), list):
        raise ValueError("invalid fixture events")
    complete = not any(value[name] for name in ("activeScopes", "dropped", "observationErrors"))
    if complete and value["entries"] != sum(event.get("event") == "entry" for event in value["events"]):
        raise ValueError("fixture entry count mismatch")


def run(adb, serial, pid, script_path, script_sha, apk, apk_sha, output, frida,
        reuse=False, prepare_local_backup=False):
    source, application = script_path.read_bytes(), apk.read_bytes()
    if hashlib.sha256(source).hexdigest() != script_sha or hashlib.sha256(application).hexdigest() != apk_sha:
        raise ValueError("fixture input SHA mismatch")
    output.mkdir(mode=0o700, exist_ok=False)
    record = {"schema": "nashura.backup-fixture-run.v1", "serial": serial,
              "script_sha256": script_sha, "apk_sha256": apk_sha,
              "commands": [], "requests_issued": 0, "catalog_ready_pass": False,
              "status": "FAILED"}
    session = None
    script = None

    def command(arguments):
        try:
            result = subprocess.run([str(adb), "-s", serial, *arguments],
                                    capture_output=True, timeout=45)
        except subprocess.TimeoutExpired as error:
            index = len(record["commands"])
            stdout, stderr = error.stdout or b"", error.stderr or b""
            (output / ("command-%02d.stdout" % index)).write_bytes(stdout)
            (output / ("command-%02d.stderr" % index)).write_bytes(stderr)
            record["commands"].append({"arguments": arguments, "exit_code": None,
                "timeout_seconds": 45, "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest()})
            raise
        index = len(record["commands"])
        name = "command-%02d" % index
        (output / (name + ".stdout")).write_bytes(result.stdout)
        (output / (name + ".stderr")).write_bytes(result.stderr)
        record["commands"].append({"arguments": arguments, "exit_code": result.returncode,
                                   "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                                   "stderr_sha256": hashlib.sha256(result.stderr).hexdigest()})
        if result.returncode:
            raise ValueError("adb fixture operation failed: " + str(index))
        return result.stdout

    try:
        avd = command(["emu", "avd", "name"]).decode().splitlines()[0].strip()
        if avd != "nashura-e2-backup-contract-api30":
            raise ValueError("exclusive fixture AVD required")
        record["avd"] = avd
        record["fingerprint"] = command(["shell", "getprop", "ro.build.fingerprint"]).decode().strip()
        record["identity_before"] = identity(adb, serial, pid)
        device = frida.get_device(serial, timeout=15)
        if {process.pid: process.name for process in device.enumerate_processes()}.get(pid) != "system_server":
            raise ValueError("Frida system_server identity mismatch")
        session = device.attach(pid)
        script = session.create_script(source.decode())
        script.load()
        record["readiness"] = script.exports_sync.install()
        validate(record["readiness"], pid, readiness=True)
        existing = command(["shell", "pm", "list", "packages", PACKAGE]).decode().strip()
        if existing:
            if not reuse or existing != "package:" + PACKAGE:
                raise ValueError("fixture already installed; explicit reuse required")
            package_path = command(["shell", "pm", "path", PACKAGE]).decode().strip()
            if not package_path.startswith("package:/data/app/") or "\n" in package_path:
                raise ValueError("unexpected installed fixture path")
            installed_bytes = command(["exec-out", "cat", package_path[len("package:"):]])
            if hashlib.sha256(installed_bytes).hexdigest() != apk_sha:
                raise ValueError("installed fixture SHA differs")
            record["reused_fixture"] = True
        else:
            installed = command(["install", "--no-incremental", str(apk)])
            if b"Success" not in [line.strip() for line in installed.splitlines()]:
                raise ValueError("fixture install did not report Success")
        time.sleep(2)
        record["after_install"] = script.exports_sync.snapshot()
        validate(record["after_install"], pid)
        if prepare_local_backup:
            record["before_prepare"] = script.exports_sync.inspect()
            command(["shell", "bmgr", "list", "transports"])
            command(["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"])
            command(["shell", "bmgr", "enable", "true"])
            command(["shell", "bmgr", "run"])
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                state = script.exports_sync.inspect()
                if state["service"]["targetPending"] is False:
                    record["after_prepare"] = state
                    break
                time.sleep(0.5)
            else:
                record["after_prepare"] = state
                raise ValueError("normal local Backup pass did not drain fixture")
        for request_index in range(2):
            previous_entries = script.exports_sync.snapshot()["entries"]
            record["requests_issued"] += 1
            returned = command(["shell", "am", "broadcast", "--include-stopped-packages",
                "-n", PACKAGE + "/.ProbeReceiver", "-a", PACKAGE + ".REQUEST"])
            if b'data="REQUEST_RETURNED"' not in returned:
                raise ValueError("fixture receiver did not confirm request return")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                snapshot = script.exports_sync.snapshot()
                validate(snapshot, pid)
                requests = sum(event.get("event") == "binder_request" for event in snapshot["events"])
                if (requests >= request_index + 1 and snapshot["activeScopes"] == 0 and
                        snapshot["entries"] > previous_entries):
                    break
                time.sleep(0.1)
            else:
                raise ValueError("fixture Binder request not observed")
            time.sleep(0.5)
            record["after_request_%d" % request_index] = script.exports_sync.snapshot()
        record["observation"] = script.exports_sync.snapshot()
        validate(record["observation"], pid)
        record["identity_after"] = identity(adb, serial, pid)
        if record["identity_after"] != record["identity_before"]:
            raise ValueError("system_server identity changed")
        if source != script_path.read_bytes() or application != apk.read_bytes():
            raise ValueError("fixture inputs changed")
        observed = record["observation"]
        record["status"] = ("INCOMPLETE_COVERAGE" if any(observed[name] for name in
            ("dropped", "observationErrors", "activeScopes")) else "RUNTIME_OBSERVED_REQUIRES_REVIEW")
    except BaseException as error:
        record["error"] = str(error)
        if script is not None:
            try:
                record["failure_observation"] = script.exports_sync.snapshot()
            except Exception as snapshot_error:
                record["snapshot_error"] = str(snapshot_error)
        raise
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception as error:
                record["detach_error"] = str(error)
                record["status"] = "INCOMPLETE_COVERAGE"
        with os.fdopen(os.open(output / "result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
    return record


def main():
    import frida

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "script", "apk", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("serial", "script-sha256", "apk-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--issue-fixture-requests", action="store_true", required=True)
    parser.add_argument("--reuse-fixture", action="store_true")
    parser.add_argument("--prepare-local-backup", action="store_true")
    args = parser.parse_args()
    result = run(args.adb, args.serial, args.pid, args.script, args.script_sha256,
                 args.apk, args.apk_sha256, args.output, frida, reuse=args.reuse_fixture,
                 prepare_local_backup=args.prepare_local_backup)
    print(json.dumps({"status": result["status"], "requests_issued": result["requests_issued"],
                      "catalog_ready_pass": False}))


if __name__ == "__main__":
    main()
