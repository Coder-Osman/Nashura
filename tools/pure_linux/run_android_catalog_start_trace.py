"""Trace only an owned isolated Android startup; preserve incomplete coverage."""

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import threading
import time

from provider_job import ENGINE_SHA, entry_guards


def main():
    import frida
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "engine", "script", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    entry_guards(args.engine)
    args.output.mkdir(mode=0o700, exist_ok=False)
    report = {"schema": "nashura.android-catalog-start-trace.v1", "status": "FAILED", "catalog_ready_pass": False,
              "engine_sha256": ENGINE_SHA, "script_sha256": hashlib.sha256(args.script.read_bytes()).hexdigest(),
              "spawn": {}, "coverage": "UNKNOWN"}
    session = None
    script = None
    pid = None
    device = None
    thread = None
    start_identity = None

    def adb(*argv, check=True):
        run = subprocess.run([str(args.adb), "-s", "emulator-5630", *argv], capture_output=True, timeout=20)
        if check and run.returncode:
            raise ValueError("adb command failed: " + argv[0])
        return run

    def capture_update(label):
        raw = adb("shell", "cd /data/user/0/nullsroyale.rel.free/update && find . -type f -exec sha256sum {} \\;").stdout
        rows = sorted(raw.decode().splitlines())
        if not rows:
            raise ValueError("empty update inventory")
        target = args.output / ("update-" + label + ".json")
        target.write_text(json.dumps(rows))
        report["update_" + label] = {"count": len(rows), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    try:
        if adb("emu", "avd", "name").stdout.decode().splitlines()[0] != "e1-only":
            raise ValueError("unexpected isolated AVD")
        apk = adb("shell", "pm", "path", "nullsroyale.rel.free").stdout.decode().strip().removeprefix("package:")
        engine = str(Path(apk).parent / "lib/arm64/libg.so")
        hashes = adb("shell", "sha256sum", apk, engine).stdout.decode().splitlines()
        if [row.split()[0] for row in hashes] != ["f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b", ENGINE_SHA]:
            raise ValueError("installed identity mismatch")
        if adb("shell", "pidof", "nullsroyale.rel.free:engine0", check=False).returncode == 0:
            raise ValueError("existing engine process")
        capture_update("before")
        device = frida.get_device_manager().add_remote_device("127.0.0.1:27066")

        def spawn():
            try:
                report["spawn"]["pid"] = device.spawn("nullsroyale.rel.free", activity="com.supercell.clashroyale.Engine0App")
            except Exception as error:
                report["spawn"]["error"] = type(error).__name__ + ": " + str(error)

        thread = threading.Thread(target=spawn)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            process = adb("shell", "pidof", "nullsroyale.rel.free:engine0", check=False)
            candidate = process.stdout.decode().strip()
            if process.returncode == 0 and candidate.isdigit():
                pid = int(candidate)
                break
            time.sleep(0.05)
        if pid is None:
            raise ValueError("owned startup process absent")
        report["owned_pid"] = pid
        start_identity = adb("shell", "cat", "/proc/%d/stat" % pid).stdout.decode().strip()
        session = device.attach(pid)
        script = session.create_script(args.script.read_text())
        script.load()
        report["installation"] = script.exports_sync.install()
        try:
            device.resume(pid)
            report["resume"] = "returned"
        except Exception as error:
            report["resume"] = type(error).__name__ + ": " + str(error)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 28996), timeout=2) as connection:
                    connection.sendall(b"status\n")
                    status = json.loads(connection.makefile("rb").readline())
                if status.get("coldReady") and status.get("contentPublished"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        else:
            raise ValueError("cold-ready status not reached")
        report["trace"] = script.exports_sync.snapshot()
        capture_update("after")
        report["update_unchanged"] = report["update_before"]["sha256"] == report["update_after"]["sha256"]
        report["status"] = "RUNTIME_OBSERVED_REQUIRES_AUDIT"
        with (args.output / "observation.json").open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        if script is not None:
            try:
                report["failure_trace"] = script.exports_sync.snapshot()
            except Exception as snapshot_error:
                report["snapshot_error"] = str(snapshot_error)
        raise
    finally:
        try:
            if pid is not None and start_identity is not None:
                current = adb("shell", "cat", "/proc/%d/stat" % pid, check=False)
                if current.returncode == 0:
                    # PID starttime is field 22; the command name can contain spaces.
                    original = start_identity.rsplit(')', 1)[1].split()[19]
                    observed = current.stdout.decode().strip().rsplit(')', 1)[1].split()[19]
                    if original != observed:
                        raise ValueError("owned process starttime changed")
                    adb("shell", "kill", str(pid))
            if thread is not None:
                thread.join(timeout=25)
                report["spawn_thread_alive"] = thread.is_alive()
        except Exception as error:
            report["cleanup_error"] = type(error).__name__ + ": " + str(error)
            report["status"] = "INCOMPLETE_CLEANUP"
        finally:
            with (args.output / "result.json").open("x") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")


if __name__ == "__main__":
    main()
