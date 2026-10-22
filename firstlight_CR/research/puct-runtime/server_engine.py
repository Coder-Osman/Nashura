"""Task-owned Linux native engines using the verified resident runtime."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
RESIDENT = Path(os.environ.get(
    "PUCT_RESIDENT_ROOT", str(ROOT.with_name("standalone-resident-20260927"))
)).resolve()


def runtime():
    spec = importlib.util.spec_from_file_location("hog_resident_service", RESIDENT / "service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resources() -> dict:
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    result = {"memory_available_gib": int(memory["MemAvailable"].split()[0]) / 1024**2,
              "disk_free_gib": shutil.disk_usage(ROOT).free / 1024**3}
    if result["memory_available_gib"] < 32 or result["disk_free_gib"] < 20:
        raise RuntimeError("resource reserve failed: " + json.dumps(result))
    return result


def owned_alive(service, record: dict, port: int) -> bool:
    if not service.alive(record):
        return False
    argv = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b"\0")
    return os.fsencode(str(Path(__file__).resolve())) in argv and b"_serve" in argv and str(port).encode() in argv


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status", "_serve"))
    parser.add_argument("--port", type=int, default=29920)
    args = parser.parse_args()
    if not 29920 <= args.port < 29945:
        raise ValueError("port outside this task's allocation")
    service = runtime()
    instance = ROOT / "engines" / str(args.port)
    instance.mkdir(parents=True, exist_ok=True)
    path = instance / "process.json"
    if args.action == "_serve":
        service.serve(args.port, instance)
        return
    record = service.load(path)
    if args.action == "start":
        if (ROOT / "STOP").exists() or (ROOT / "BOUNDARY_STOP").exists():
            raise RuntimeError("task stop marker is present")
        resources()
        with (instance / "launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record = service.load(path)
            if service.alive(record) and not owned_alive(service, record, args.port):
                raise RuntimeError("engine process ownership mismatch")
            if not owned_alive(service, record, args.port):
                if not service.port_available(args.port):
                    raise RuntimeError("task engine port is already in use")
                with (instance / "supervisor.log").open("a") as output:
                    child = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "_serve", "--port", str(args.port)],
                        cwd=ROOT, stdout=output, stderr=output, start_new_session=True)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    record = service.load(path)
                    if record.get("pid") == child.pid and record.get("ready") and owned_alive(service, record, args.port):
                        break
                    if child.poll() is not None:
                        raise RuntimeError("task engine failed to start; inspect " + str(instance))
                    time.sleep(.25)
                else:
                    if owned_alive(service, record, args.port):
                        os.kill(record["pid"], signal.SIGTERM)
                    raise TimeoutError("task engine startup timed out")
    elif args.action == "stop":
        if service.alive(record) and not owned_alive(service, record, args.port):
            raise RuntimeError("refusing stop after engine ownership changed")
        if owned_alive(service, record, args.port):
            os.kill(record["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not service.alive(record) and service.port_available(args.port):
                    break
                time.sleep(.1)
            else:
                raise TimeoutError("task engine shutdown incomplete")
            record = service.load(path)
    result = {key: record.get(key) for key in (
        "pid", "start_ticks", "child_pid", "child_start_ticks", "port", "phase", "ready", "ready_seconds")}
    result["alive"] = owned_alive(service, record, args.port)
    result["task"] = ROOT.name
    if result["alive"] and record.get("ready"):
        result["status"] = service.request(args.port, "status")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
