"""Capture an isolated Android baseline catalog layout; never invoke game actions."""

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

from provider_job import ENGINE_SHA, entry_guards
from compare_catalog_update_assets import parse_inventory


def update_inventory(adb_output):
    rows = sorted(adb_output.splitlines())
    if not rows:
        raise ValueError("empty update inventory")
    return rows


def main():
    import frida

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "engine", "script", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--frida-address", required=True)
    parser.add_argument("--control-port", type=int, required=True)
    parser.add_argument("--owned-pid", type=int, help="Process already created by this isolated experiment")
    parser.add_argument("--update-reference", type=Path)
    parser.add_argument("--update-reference-sha256")
    parser.add_argument("--recover-spawn-timeout", action="store_true")
    parser.add_argument("--inspect-row-ids", action="store_true")
    args = parser.parse_args()
    if args.serial != "emulator-5630":
        raise ValueError("dedicated experiment serial required")
    if bool(args.update_reference) != bool(args.update_reference_sha256):
        raise ValueError("update reference and identity required together")
    reference = None
    if args.update_reference:
        if hashlib.sha256(args.update_reference.read_bytes()).hexdigest() != args.update_reference_sha256:
            raise ValueError("update reference identity mismatch")
        reference = parse_inventory(args.update_reference)
    guard = entry_guards(args.engine)["CONTEXT_GET"]
    args.output.mkdir(mode=0o700, exist_ok=False)
    report = {"schema": "nashura.android-catalog-layout-capture.v1", "status": "FAILED",
              "engine_sha256": ENGINE_SHA, "script_sha256": hashlib.sha256(args.script.read_bytes()).hexdigest(),
              "catalog_ready_pass": False, "commands": []}
    session = None
    device = None
    pid = None

    def adb(*command):
        value = subprocess.run([str(args.adb), "-s", args.serial, *command], capture_output=True, timeout=30)
        report["commands"].append({"argv": command, "exit": value.returncode,
            "stdout_sha256": hashlib.sha256(value.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(value.stderr).hexdigest()})
        if value.returncode:
            raise ValueError("adb command failed")
        return value.stdout.decode().strip()

    try:
        if adb("emu", "avd", "name").splitlines()[0] != "e1-only":
            raise ValueError("unexpected AVD")
        package = adb("shell", "pm", "path", "nullsroyale.rel.free")
        if not package.startswith("package:"):
            raise ValueError("game package absent")
        apk = package[len("package:"):]
        remote_engine = str(Path(apk).parent / "lib/arm64/libg.so")
        identities = adb("shell", "sha256sum", apk, remote_engine).splitlines()
        if [item.split()[0] for item in identities] != [
                "f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b", ENGINE_SHA]:
            raise ValueError("installed identity mismatch")
        if reference is not None:
            rows = update_inventory(adb("shell", "cd /data/user/0/nullsroyale.rel.free/update && find . -type f -exec sha256sum {} \\;"))
            (args.output / "update-before.json").write_text(json.dumps(rows))
            if parse_inventory(args.output / "update-before.json") != reference:
                raise ValueError("update inventory before capture differs")
            report["update_reference_sha256"] = args.update_reference_sha256
            report["update_count"] = len(rows)
        device = frida.get_device_manager().add_remote_device(args.frida_address)
        existing = subprocess.run([str(args.adb), "-s", args.serial, "shell", "pidof",
                                   "nullsroyale.rel.free:engine0"], capture_output=True)
        if args.owned_pid is not None:
            if existing.returncode or existing.stdout.decode().strip() != str(args.owned_pid):
                raise ValueError("explicit owned process identity differs")
            pid = args.owned_pid
        else:
            if existing.returncode == 0:
                raise ValueError("existing engine process")
            try:
                pid = device.spawn("nullsroyale.rel.free", activity="com.supercell.clashroyale.Engine0App")
            except frida.TimedOutError:
                if not args.recover_spawn_timeout:
                    raise
                report["spawn_timed_out"] = True
                observed = adb("shell", "pidof", "nullsroyale.rel.free:engine0")
                if not observed.isdigit():
                    raise ValueError("timed-out spawn did not yield one engine PID")
                pid = int(observed)
        report["owned_pid"] = pid
        session = device.attach(pid)
        script = session.create_script(args.script.read_text())
        script.load()
        if args.owned_pid is None and not report.get("spawn_timed_out"):
            device.resume(pid)
        deadline = time.monotonic() + 90
        status = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", args.control_port), timeout=2) as connection:
                    connection.sendall(b"status\n")
                    status = json.loads(connection.makefile("rb").readline())
                if status.get("coldReady") and status.get("contentReady") and status.get("contentPublished"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise ValueError("cold-ready state not observed")
        if adb("shell", "pidof", "nullsroyale.rel.free:engine0") != str(pid):
            raise ValueError("owned engine identity changed")
        first = script.exports_sync.snapshot(status["contentContext"], guard, args.inspect_row_ids)
        second = script.exports_sync.snapshot(status["contentContext"], guard, args.inspect_row_ids)
        if first != second:
            raise ValueError("catalog layout changed between snapshots")
        report["layout"] = first
        if reference is not None:
            rows = update_inventory(adb("shell", "cd /data/user/0/nullsroyale.rel.free/update && find . -type f -exec sha256sum {} \\;"))
            (args.output / "update-after.json").write_text(json.dumps(rows))
            if parse_inventory(args.output / "update-after.json") != reference:
                raise ValueError("update inventory after capture differs")
            report["update_verified"] = True
        report["status"] = "RUNTIME_OBSERVED_LIMITED"
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        try:
            if session is not None:
                session.detach()
            if pid is not None and device is not None:
                device.kill(pid)
        finally:
            with (args.output / "result.json").open("x") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")


if __name__ == "__main__":
    main()
