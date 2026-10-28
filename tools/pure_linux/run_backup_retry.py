"""Observe one owned agent failure and one queue retry on the exclusive AVD."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from backup_payload_audit import installed_path, journal_paths
from run_backup_branch_passive import identity
from run_backup_payload import AVD


PACKAGE = "org.nashura.backupretry"


def run(adb, serial, apk, apk_sha, platform_sha, token, output):
    if serial != "emulator-5582":
        raise ValueError("exclusive retry serial required")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("32 hex characters required")
    if hashlib.sha256(apk.read_bytes()).hexdigest() != apk_sha:
        raise ValueError("local APK identity differs")
    output.mkdir(mode=0o700, exist_ok=False)
    record = {"schema": "nashura.backup-retry-run.v2", "status": "FAILED",
              "file_capture_mode": "shell_T_remote_exit_status",
              "serial": serial, "token": token, "apk_sha256": apk_sha,
              "platform_sha256": platform_sha, "commands": [], "snapshots": {},
              "stores_issued": 0, "requests_issued": 0, "queue_runs_issued": 0,
              "game_stores_issued": 0, "system_server_hooks": 0,
              "backupnow_issued": 0, "catalog_ready_pass": False}

    def command(arguments, required=True):
        index = len(record["commands"])
        try:
            result = subprocess.run([str(adb), "-s", serial, *arguments], capture_output=True, timeout=45)
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, code = error.stdout or b"", error.stderr or b"", None
        for extension, data in (("stdout", stdout), ("stderr", stderr)):
            (output / ("command-%03d.%s" % (index, extension))).write_bytes(data)
        record["commands"].append({"arguments": arguments, "exit_code": code,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest()})
        if required and code != 0:
            raise ValueError("ADB command failed: %d" % index)
        return stdout

    def snapshot(name):
        indices = {"dump": len(record["commands"])}
        command(["shell", "dumpsys", "backup"])
        indices["journal_listing"] = len(record["commands"])
        paths = journal_paths(command(["shell", "find", "/data/backup/pending", "-type", "f"]))
        indices["journals"] = []
        for path in sorted(paths):
            indices["journals"].append(len(record["commands"]))
            command(["exec-out", "cat", path])
        indices["files"] = []
        for path in ("/data/user/0/" + PACKAGE + "/shared_prefs/fixture.xml",
                     "/data/user/0/" + PACKAGE + "/no_backup/fail-once",
                     "/data/user/0/com.android.localtransport/files/1/_delta/" + PACKAGE + "/Zml4dHVyZTpmaXh0dXJl",
                     "/data/backup/com.android.localtransport.LocalTransport/" + PACKAGE):
            indices["files"].append(len(record["commands"]))
            command(["shell", "-T", "cat", path], required=False)
        indices["log"] = len(record["commands"])
        command(["logcat", "-d", "-v", "threadtime", "-b", "all", "-t", "6000"])
        record["snapshots"][name] = indices

    try:
        if command(["emu", "avd", "name"]).decode().splitlines()[0] != AVD:
            raise ValueError("exclusive fixture AVD required")
        record["avd"] = AVD
        if command(["shell", "pm", "list", "packages", "nullsroyale.rel.free"]).strip():
            raise ValueError("game present on fixture AVD")
        if command(["shell", "pm", "list", "packages", PACKAGE]).strip():
            raise ValueError("fresh retry package required; preserve previous data")
        record["fingerprint"] = command(["shell", "getprop", "ro.build.fingerprint"]).decode().strip()
        if hashlib.sha256(command(["exec-out", "cat", "/system/framework/services.jar"])).hexdigest() != platform_sha:
            raise ValueError("platform identity differs")
        pid = int(command(["shell", "pidof", "system_server"]).strip())
        record["identity_before"] = identity(adb, serial, pid)
        if b"Success" not in command(["install", str(apk)]):
            raise ValueError("fixture installation failed")
        apk_path = installed_path(command(["shell", "pm", "path", PACKAGE]))
        if hashlib.sha256(command(["exec-out", "cat", apk_path])).hexdigest() != apk_sha:
            raise ValueError("installed APK differs")
        command(["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"])
        command(["shell", "bmgr", "enable", "true"])
        record["queue_runs_issued"] += 1
        command(["shell", "bmgr", "run"])
        time.sleep(6)
        snapshot("before")
        record["stores_issued"] = record["requests_issued"] = 1
        response = command(["shell", "am", "broadcast", "--include-stopped-packages",
            "-n", PACKAGE + "/.RetryReceiver", "-a", PACKAGE + ".STORE_AND_REQUEST", "--es", "token", token])
        if ("RETRY_REQUEST_RETURNED:" + token).encode() not in response:
            raise ValueError("store/request not confirmed")
        snapshot("queued")
        for name in ("after_failure", "after_retry"):
            record["queue_runs_issued"] += 1
            command(["shell", "bmgr", "run"])
            time.sleep(8)
            snapshot(name)
        record["identity_after"] = identity(adb, serial, pid)
        if record["identity_before"] != record["identity_after"]:
            raise ValueError("system_server identity changed")
        for path, expected in ((apk_path, apk_sha), ("/system/framework/services.jar", platform_sha)):
            if hashlib.sha256(command(["exec-out", "cat", path])).hexdigest() != expected:
                raise ValueError("input changed")
        record["status"] = "RUNTIME_OBSERVED_REQUIRES_REVIEW"
    except BaseException as error:
        record["error"] = str(error)
        raise
    finally:
        with os.fdopen(os.open(output / "result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "apk", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("serial", "apk-sha256", "platform-sha256", "token"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = run(args.adb, args.serial, args.apk, args.apk_sha256, args.platform_sha256, args.token, args.output)
    print(json.dumps({"status": result["status"], "catalog_ready_pass": False}))


if __name__ == "__main__":
    main()
