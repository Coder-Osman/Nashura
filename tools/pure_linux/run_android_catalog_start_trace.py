"""Trace only an owned isolated Android startup; preserve incomplete coverage."""

import argparse
import hashlib
import json
from pathlib import Path
import queue
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
    parser.add_argument("--spawn-gate", action="store_true")
    parser.add_argument("--expected-batches", type=int, default=0)
    parser.add_argument("--layout-script", type=Path)
    parser.add_argument("--inspect-row-ids", action="store_true")
    parser.add_argument("--batch-table-counts", action="store_true")
    parser.add_argument("--batch-phase-counts", action="store_true")
    args = parser.parse_args()
    if args.expected_batches < 0:
        parser.error("expected-batches must be nonnegative")
    if args.inspect_row_ids and not args.layout_script:
        parser.error("inspect-row-ids requires layout-script")
    if args.batch_phase_counts and not args.batch_table_counts:
        parser.error("batch-phase-counts requires batch-table-counts")
    guards = entry_guards(args.engine)
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
    gate_enabled = False
    gate_events = queue.Queue()
    gate_spawn = None
    launch = None

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

        if args.spawn_gate:
            report["spawn_gate"] = {"events": [], "enabled": False, "disabled": False}

            def on_spawn(spawn):
                gate_events.put((spawn.pid, spawn.identifier))

            device.on("spawn-added", on_spawn)
            device.enable_spawn_gating()
            gate_enabled = True
            report["spawn_gate"]["enabled"] = True
            launch = subprocess.Popen([str(args.adb), "-s", "emulator-5630", "shell", "am", "start",
                                       "-n", "nullsroyale.rel.free/com.supercell.clashroyale.Engine0App"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    candidate, identifier = gate_events.get(timeout=0.5)
                except queue.Empty:
                    continue
                report["spawn_gate"]["events"].append({"pid": candidate, "identifier": identifier})
                if identifier == "nullsroyale.rel.free:engine0":
                    if gate_spawn is not None:
                        raise ValueError("multiple gated target processes")
                    gate_spawn = candidate
                    pid = candidate
                    break
                device.resume(candidate)
            if pid is None:
                raise ValueError("target spawn not observed by gate")
            observed = adb("shell", "pidof", "nullsroyale.rel.free:engine0").stdout.decode().strip()
            if observed != str(pid):
                raise ValueError("gated target PID differs")
        else:
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
        report["script_messages"] = []
        report["script_error_count"] = 0

        def on_message(message, data):
            if message.get("type") == "error":
                report["script_error_count"] += 1
            if len(report["script_messages"]) < 100:
                report["script_messages"].append(message)

        script.on("message", on_message)
        script.load()
        report["installation"] = script.exports_sync.install(args.batch_table_counts, args.batch_phase_counts)
        try:
            device.resume(pid)
            report["resume"] = "returned"
            gate_spawn = None
        except Exception as error:
            report["resume"] = type(error).__name__ + ": " + str(error)
            if args.spawn_gate:
                raise
        if gate_enabled:
            device.disable_spawn_gating()
            gate_enabled = False
            report["spawn_gate"]["disabled"] = True
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
        deadline = time.monotonic() + 30
        while True:
            report["trace"] = script.exports_sync.snapshot()
            if report["script_error_count"]:
                raise ValueError("Frida script callback error")
            batches = sum(event.get("kind") == "table_batch" for event in report["trace"]["events"])
            if batches >= args.expected_batches and report["trace"].get("activeBatchScopes", 0) == 0:
                break
            if time.monotonic() >= deadline:
                raise ValueError("expected completed batch observations not reached")
            time.sleep(0.25)
        if args.layout_script:
            if not status.get("contentReady") or not status.get("contentContext"):
                raise ValueError("published content context unavailable")
            report["layout_script_sha256"] = hashlib.sha256(args.layout_script.read_bytes()).hexdigest()
            layout_script = session.create_script(args.layout_script.read_text())
            layout_script.on("message", on_message)
            layout_script.load()
            first = layout_script.exports_sync.snapshot(status["contentContext"], guards["CONTEXT_GET"],
                                                        args.inspect_row_ids)
            second = layout_script.exports_sync.snapshot(status["contentContext"], guards["CONTEXT_GET"],
                                                         args.inspect_row_ids)
            if first != second:
                raise ValueError("catalog layout changed between snapshots")
            report["layout"] = first
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
            if gate_enabled:
                try:
                    if gate_spawn is not None:
                        device.kill(gate_spawn)
                        gate_spawn = None
                finally:
                    device.disable_spawn_gating()
                    gate_enabled = False
                    report["spawn_gate"]["disabled"] = True
            if pid is not None and start_identity is not None:
                current = adb("shell", "cat", "/proc/%d/stat" % pid, check=False)
                if current.returncode == 0:
                    # PID starttime is field 22; the command name can contain spaces.
                    original = start_identity.rsplit(')', 1)[1].split()[19]
                    observed = current.stdout.decode().strip().rsplit(')', 1)[1].split()[19]
                    if original != observed:
                        raise ValueError("owned process starttime changed")
                    adb("shell", "kill", str(pid))
            if launch is not None:
                try:
                    stdout, stderr = launch.communicate(timeout=10)
                    report["launch"] = {"exit": launch.returncode,
                                        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                                        "stderr_sha256": hashlib.sha256(stderr).hexdigest()}
                except subprocess.TimeoutExpired:
                    launch.kill()
                    launch.communicate()
                    report["launch"] = {"timeout": True}
            if thread is not None:
                thread.join(timeout=25)
                report["spawn_thread_alive"] = thread.is_alive()
        except Exception as error:
            report["cleanup_error"] = type(error).__name__ + ": " + str(error)
            report["status"] = "INCOMPLETE_CLEANUP"
        finally:
            if report.get("script_error_count"):
                report["status"] = "FAILED_SCRIPT_CALLBACK"
            with (args.output / "result.json").open("x") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")


if __name__ == "__main__":
    main()
