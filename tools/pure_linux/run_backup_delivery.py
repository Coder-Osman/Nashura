"""Observe one forced real JobScheduler delivery on the exclusive test AVD."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from run_backup_branch_passive import identity


PACKAGE = "org.nashura.backupfixture"
SCHEMA = "nashura.backup-delivery-observer.v1"
JOB = "com.android.server.backup.KeyValueBackupJob."
TASK = "com.android.server.backup.keyvalue.KeyValueBackupTask."
HOOKS = {
    JOB + "onStartJob(android.app.job.JobParameters)",
    JOB + "clearScheduledForUserId(int)",
    JOB + "cancel(int,android.content.Context)",
    "com.android.server.backup.UserBackupManagerService.backupNow()",
}


def validate(value, pid, readiness=False):
    if (value.get("schema") != SCHEMA or value.get("installed") is not True or
            value.get("pid") != pid):
        raise ValueError("delivery observer identity mismatch")
    if readiness:
        hooks = value.get("hooks")
        if (value.get("targetPackage") != PACKAGE or not isinstance(hooks, list) or
                len(hooks) != len(HOOKS) or set(hooks) != HOOKS):
            raise ValueError("delivery hook coverage mismatch")
    elif (any(type(value.get(name)) is not int or value[name] != 0
              for name in ("dropped", "observationErrors", "activeScopes")) or
          value.get("catalogReadyPass") is not False or not isinstance(value.get("events"), list)):
        raise ValueError("delivery observation incomplete")


def run(adb, serial, pid, source_path, expected_sha, apk_sha, platform_sha, output, frida):
    source = source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != expected_sha:
        raise ValueError("observer SHA mismatch")
    output.mkdir(mode=0o700, exist_ok=False)
    (output / "observer.js").write_bytes(source)
    record = {"schema": "nashura.backup-delivery-run.v1", "status": "FAILED",
              "serial": serial, "script_sha256": expected_sha, "apk_sha256": apk_sha,
              "platform_sha256": platform_sha, "commands": [], "requests_issued": 0,
              "forced_jobs_issued": 0, "stores_issued": 0, "catalog_ready_pass": False,
              "trigger": "controlled_forced_JobScheduler_not_natural_expiry"}
    session = None
    script = None
    messages = []

    def on_message(message, data):
        messages.append(message)
        with (output / "messages.jsonl").open("a") as stream:
            stream.write(json.dumps(message) + "\n")

    def command(arguments):
        index = len(record["commands"])
        try:
            result = subprocess.run([str(adb), "-s", serial, *arguments],
                                    capture_output=True, timeout=45)
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, code = error.stdout or b"", error.stderr or b"", None
        for extension, data in (("stdout", stdout), ("stderr", stderr)):
            (output / ("command-%02d.%s" % (index, extension))).write_bytes(data)
        record["commands"].append({"arguments": arguments, "exit_code": code,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest()})
        if code != 0:
            raise ValueError("adb command failed or timed out: %d" % index)
        return stdout

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = script.exports_sync.inspect()
            if predicate(state):
                return state
            time.sleep(0.25)
        record["timeout_state"] = state
        raise ValueError("delivery state wait expired")

    try:
        avd = command(["emu", "avd", "name"]).decode().splitlines()[0].strip()
        if avd != "nashura-e2-backup-contract-api30":
            raise ValueError("exclusive fixture AVD required")
        record["avd"] = avd
        record["fingerprint"] = command(["shell", "getprop", "ro.build.fingerprint"]).decode().strip()
        record["identity_before"] = identity(adb, serial, pid)
        if command(["shell", "pm", "list", "packages", "nullsroyale.rel.free"]).strip():
            raise ValueError("game present on fixture AVD")
        installed = command(["shell", "pm", "path", PACKAGE]).decode().strip()
        if not installed.startswith("package:/data/app/") or "\n" in installed:
            raise ValueError("installed fixture path mismatch")
        application = command(["exec-out", "cat", installed[len("package:"):]])
        if hashlib.sha256(application).hexdigest() != apk_sha:
            raise ValueError("installed fixture SHA mismatch")
        platform = command(["exec-out", "cat", "/system/framework/services.jar"])
        if hashlib.sha256(platform).hexdigest() != platform_sha:
            raise ValueError("platform SHA mismatch")
        device = frida.get_device(serial, timeout=15)
        if {process.pid: process.name for process in device.enumerate_processes()}.get(pid) != "system_server":
            raise ValueError("Frida system_server identity mismatch")
        session = device.attach(pid)
        script = session.create_script(source.decode())
        script.on("message", on_message)
        script.load()
        record["readiness"] = script.exports_sync.install()
        validate(record["readiness"], pid, readiness=True)
        record["before_prepare"] = script.exports_sync.inspect()
        command(["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"])
        command(["shell", "bmgr", "enable", "true"])
        command(["shell", "bmgr", "run"])
        record["after_prepare"] = wait_for(lambda state: state["pending"] is False, 30)
        time.sleep(2)
        record["prelude"] = script.exports_sync.snapshot()
        validate(record["prelude"], pid)
        record["experiment_start_sequence"] = len(record["prelude"]["events"])
        record["requests_issued"] = 1
        response = command(["shell", "am", "broadcast", "--include-stopped-packages",
                            "-n", PACKAGE + "/.ProbeReceiver", "-a", PACKAGE + ".REQUEST"])
        if b'data="REQUEST_RETURNED"' not in response:
            raise ValueError("fixture request did not confirm return")
        record["queued"] = wait_for(lambda state: state["pending"] is True and len(state["jobs"]) == 1, 10)
        selected = record["queued"]["jobs"][0]
        if type(selected["id"]) is not int or selected["userId"] != 0:
            raise ValueError("job identity mismatch")
        record["forced_jobs_issued"] = 1
        command(["shell", "cmd", "jobscheduler", "run", "-f", "--user", "0", "android", str(selected["id"])])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            observed = script.exports_sync.snapshot()
            events = observed["events"][record["experiment_start_sequence"]:]
            if (observed["activeScopes"] == 0 and any(event["event"] == "return" and
                    event["details"].get("method") == JOB + "onStartJob(android.app.job.JobParameters)"
                    for event in events)):
                break
            time.sleep(0.25)
        else:
            raise ValueError("job callback return not observed")
        time.sleep(10)
        record["observation"] = script.exports_sync.snapshot()
        validate(record["observation"], pid)
        record["after_delivery"] = script.exports_sync.inspect()
        command(["shell", "dumpsys", "backup"])
        command(["logcat", "-d", "-b", "all", "-t", "2000"])
        record["identity_after"] = identity(adb, serial, pid)
        if record["identity_before"] != record["identity_after"] or source != source_path.read_bytes():
            raise ValueError("identity changed during delivery")
        record["status"] = "RUNTIME_OBSERVED_REQUIRES_REVIEW"
    except BaseException as error:
        record["error"] = str(error)
        if script is not None:
            try:
                record["failure_observation"] = script.exports_sync.snapshot()
            except Exception as snapshot_error:
                record["snapshot_error"] = str(snapshot_error)
        raise
    finally:
        record["frida_messages"] = messages
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
    for name in ("adb", "script", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("serial", "script-sha256", "apk-sha256", "platform-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--force-fixture-job", action="store_true", required=True)
    args = parser.parse_args()
    result = run(args.adb, args.serial, args.pid, args.script, args.script_sha256,
                 args.apk_sha256, args.platform_sha256, args.output, frida)
    print(json.dumps({"status": result["status"], "catalog_ready_pass": False}))


if __name__ == "__main__":
    main()
