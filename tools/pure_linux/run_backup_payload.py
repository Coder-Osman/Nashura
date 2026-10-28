"""Observe a one-shot nonempty backup on the exclusive fixture AVD, without hooks."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from run_backup_branch_passive import identity


PACKAGE = "org.nashura.backuppayload"
AVD = "nashura-e2-backup-contract-api30"


def run(adb, serial, apk, apk_sha, platform_sha, token, output):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("32 hex characters required")
    if hashlib.sha256(apk.read_bytes()).hexdigest() != apk_sha:
        raise ValueError("local APK identity differs")
    output.mkdir(mode=0o700, exist_ok=False)
    record = {"schema": "nashura.backup-payload-run.v1", "status": "FAILED",
              "serial": serial, "token": token, "apk_sha256": apk_sha,
              "platform_sha256": platform_sha, "commands": [],
              "requests_issued": 0, "stores_issued": 0, "backupnow_issued": 0,
              "game_stores_issued": 0, "system_server_hooks": 0,
              "catalog_ready_pass": False,
              "trigger": "owned_payload_then_bmgr_backupnow_nonincremental"}

    def command(arguments, required=True):
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
        if required and code != 0:
            raise ValueError("ADB command failed: %d" % index)
        return stdout

    try:
        if command(["emu", "avd", "name"]).decode().splitlines()[0].strip() != AVD:
            raise ValueError("exclusive fixture AVD required")
        record["avd"] = AVD
        if command(["shell", "pm", "list", "packages", "nullsroyale.rel.free"]).strip():
            raise ValueError("game present on fixture AVD")
        if command(["shell", "pm", "list", "packages", PACKAGE]).strip():
            raise ValueError("fresh payload package required; preserve existing data")
        record["fingerprint"] = command(["shell", "getprop", "ro.build.fingerprint"]).decode().strip()
        platform = command(["exec-out", "cat", "/system/framework/services.jar"])
        if hashlib.sha256(platform).hexdigest() != platform_sha:
            raise ValueError("platform identity differs")
        pid = int(command(["shell", "pidof", "system_server"]).strip())
        record["identity_before"] = identity(adb, serial, pid)
        if b"Success" not in command(["install", str(apk)]):
            raise ValueError("fixture install failed")
        installed = command(["shell", "pm", "path", PACKAGE]).decode().strip()
        if not installed.startswith("package:/data/app/") or "\n" in installed:
            raise ValueError("installed fixture path differs")
        installed_bytes = command(["exec-out", "cat", installed[len("package:"):]])
        if hashlib.sha256(installed_bytes).hexdigest() != apk_sha:
            raise ValueError("installed APK identity differs")
        command(["shell", "bmgr", "transport", "com.android.localtransport/.LocalTransport"])
        command(["shell", "bmgr", "enable", "true"])
        command(["shell", "bmgr", "run"])
        time.sleep(5)
        record["before_dump_index"] = len(record["commands"])
        command(["shell", "dumpsys", "backup"])
        record["prelude_log_index"] = len(record["commands"])
        command(["logcat", "-d", "-b", "all", "-t", "2000"])
        record["requests_issued"] = 1
        record["stores_issued"] = 1
        response = command(["shell", "am", "broadcast", "--include-stopped-packages",
                            "-n", PACKAGE + "/.PayloadReceiver", "-a", PACKAGE + ".STORE_AND_REQUEST",
                            "--es", "token", token])
        if ("PAYLOAD_REQUEST_RETURNED:" + token).encode() not in response:
            raise ValueError("fixture did not confirm store/request")
        record["queued_dump_index"] = len(record["commands"])
        command(["shell", "dumpsys", "backup"])
        record["backupnow_issued"] = 1
        record["backupnow_index"] = len(record["commands"])
        command(["shell", "bmgr", "backupnow", "--non-incremental", PACKAGE])
        time.sleep(3)
        record["after_dump_index"] = len(record["commands"])
        command(["shell", "dumpsys", "backup"])
        record["final_log_index"] = len(record["commands"])
        command(["logcat", "-d", "-b", "all", "-t", "5000"])
        record["backup_files_index"] = len(record["commands"])
        command(["shell", "find", "/data/backup", "/data/user_de/0/com.android.localtransport",
                 "-type", "f"], required=False)
        record["identity_after"] = identity(adb, serial, pid)
        if record["identity_before"] != record["identity_after"]:
            raise ValueError("system_server identity changed")
        if hashlib.sha256(command(["exec-out", "cat", installed[len("package:"):]])).hexdigest() != apk_sha:
            raise ValueError("installed APK changed")
        if hashlib.sha256(command(["exec-out", "cat", "/system/framework/services.jar"])).hexdigest() != platform_sha:
            raise ValueError("platform changed")
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
    result = run(args.adb, args.serial, args.apk, args.apk_sha256,
                 args.platform_sha256, args.token, args.output)
    print(json.dumps({"status": result["status"], "catalog_ready_pass": False}))


if __name__ == "__main__":
    main()
