"""Task-owned, resource-bounded host queue for the finite PUCT prototype batch."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from prototype_contract import CONCURRENCY, HOST_ORDER, jobs, validate_result
from server_engine import owned_alive, resources, runtime

ROOT = Path(__file__).resolve().parent
BATCH = ROOT / "formal/prototype"
RECORD = BATCH / "worker-process.json"
STATUS = BATCH / "worker-status.json"


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def process_arguments(pid):
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.getuid():
            return []
        return [os.fsdecode(value) for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if value]
    except OSError:
        return []


def matches_job(arguments, job):
    try:
        if len(arguments) < 2 or Path(arguments[1]).resolve() != ROOT / "event_puct_acceptance.py":
            return False
        for name in ("port", "device", "seed", "episode_id"):
            option = "--" + name.replace("_", "-")
            if arguments[arguments.index(option) + 1] != str(job[name]):
                return False
        output = arguments[arguments.index("--output") + 1]
        return Path(output).resolve() == (ROOT / job["output"]).resolve()
    except (ValueError, IndexError, OSError):
        return False


def running_processes(planned):
    result = {}
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        pid = int(path.parent.name)
        arguments = process_arguments(pid)
        if "--episode-id" not in arguments:
            continue
        for job in planned:
            if matches_job(arguments, job):
                result.setdefault(job["episode_id"], []).append(pid)
    return result


def inspect_jobs(host, *, attempted=(), cache=None):
    config = json.loads((ROOT / "config.json").read_text())
    plan = json.loads((ROOT / "match-plan.json").read_text())
    planned = [job for job in jobs(config) if job["host"] == host]
    running = running_processes(planned)
    result = []
    for job in planned:
        output = ROOT / job["output"]
        state = {**job, "state": "pending", "verified": False, "pids": running.get(job["episode_id"], [])}
        if output.exists():
            signature = (output.stat().st_mtime_ns, output.stat().st_size)
            entry = cache.get(str(output)) if cache is not None else None
            if entry is None or entry[0] != signature:
                try:
                    data = json.loads(output.read_text())
                    from runtime_contract import PROBE_SHA256
                    validate_result(data, job, config, plan, native_runtime_sha256=PROBE_SHA256)
                    evidence = {"state": "passed", "verified": True, "passed": True, "simulations": 32,
                                "ended": True, "truncated": False,
                                "result_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                                "terminal_tick": data["episode"]["terminal_tick"]}
                except Exception as error:
                    evidence = {"state": "invalid", "error": str(error)}
                entry = (signature, evidence)
                if cache is not None:
                    cache[str(output)] = entry
            state.update(entry[1])
        elif len(state["pids"]) > 1:
            state.update(state="conflict", error="multiple collectors match the same episode")
        elif state["pids"]:
            state["state"] = "running"
        elif job["episode_id"] in attempted:
            state.update(state="failed", error="collector exited without a verified result")
        progress_path = output.with_suffix(".progress.json")
        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text())
                state.update(progress_phase=progress.get("phase"), tick=progress.get("tick"))
            except (OSError, ValueError):
                pass
        result.append(state)
    return result


def available_slots(states, limit, memory_gib):
    running = sum(len(state["pids"]) for state in states)
    by_memory = max(0, int((memory_gib - 32) // 2.5))
    return max(0, min(limit - running, by_memory))


def start_job(job, service):
    port = job["port"]
    record = service.load(ROOT / "engines" / str(port) / "process.json")
    if not record.get("ready") or not owned_alive(service, record, port):
        raise RuntimeError(f"task-owned native engine {port} is not ready")
    status = service.request(port, "status")
    if (status.get("ok") is not True or status.get("coldReady") is not True or
            status.get("contentReady") is not True or status.get("snapshotHandles") != 0):
        raise RuntimeError(f"native engine {port} is not clean and ready")
    command = [sys.executable, str(ROOT / "event_puct_acceptance.py"), "--port", str(port),
               "--device", job["device"], "--seed", str(job["seed"]), "--episode-id", job["episode_id"],
               "--simulations", "32", "--output", str(ROOT / job["output"])]
    with (ROOT / job["log"]).open("a") as log:
        log.write(json.dumps({"event": "prototype-attempt", "started_at": time.time(), "job": job}) + "\n")
        log.flush()
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    (ROOT / job["pid"]).write_text(str(child.pid) + "\n")
    return child


def serve(host, limit):
    service = runtime()
    attempted, children, cache = set(), {}, {}
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (BATCH / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save_json(RECORD, {"pid": os.getpid(), "start_ticks": service.start_ticks(os.getpid()),
                           "host": host, "limit": limit, "started_at": time.time()})
        while not stopping:
            for child in children.values():
                child.poll()
            states = inspect_jobs(host, attempted=attempted, cache=cache)
            attempted.update(state["episode_id"] for state in states if state["state"] == "running")
            resource_error = None
            try:
                reserve = resources()
                if (ROOT / "STOP").exists() or (ROOT / "BOUNDARY_STOP").exists():
                    raise RuntimeError("task stop marker is present")
                slots = available_slots(states, limit, reserve["memory_available_gib"])
                for state in states:
                    if slots <= 0:
                        break
                    if state["state"] != "pending":
                        continue
                    reserve = resources()
                    if available_slots(states, limit, reserve["memory_available_gib"]) < 1:
                        break
                    attempted.add(state["episode_id"])
                    try:
                        child = start_job(state, service)
                        children[state["episode_id"]] = child
                        state.update(state="running", pids=[child.pid])
                        slots -= 1
                        time.sleep(2)
                    except Exception as error:
                        state.update(state="failed", error=str(error))
            except Exception as error:
                resource_error = str(error)
            counts = {name: sum(state["state"] == name for state in states)
                      for name in ("passed", "running", "pending", "failed", "invalid", "conflict")}
            save_json(STATUS, {"host": host, "checked_at": time.time(), "limit": limit, "counts": counts,
                               "resource_error": resource_error, "jobs": states})
            if not counts["pending"] and not counts["running"]:
                break
            time.sleep(15)


def start(host, limit):
    service = runtime()
    with (BATCH / "worker-launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = service.load(RECORD)
        if service.alive(record):
            arguments = process_arguments(record["pid"])
            if (str(Path(__file__).resolve()) not in arguments or "_serve" not in arguments or
                    record.get("host") != host):
                raise RuntimeError("prototype worker process ownership mismatch")
            return {"already_running": True, **record}
        with (BATCH / "worker.log").open("a") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_serve", "--host", host,
                                      "--max-concurrency", str(limit)], cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            record = service.load(RECORD)
            if record.get("pid") == child.pid and service.alive(record):
                return {"already_running": False, **record}
            if child.poll() is not None:
                raise RuntimeError("prototype host worker exited; inspect worker.log")
            time.sleep(0.2)
        raise TimeoutError("prototype host worker startup timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "_serve"))
    parser.add_argument("--host", required=True, choices=HOST_ORDER)
    parser.add_argument("--max-concurrency", type=int)
    args = parser.parse_args()
    limit = CONCURRENCY[args.host] if args.max_concurrency is None else args.max_concurrency
    if not 1 <= limit <= CONCURRENCY[args.host]:
        parser.error("concurrency exceeds this host's prototype resource limit")
    BATCH.mkdir(parents=True, exist_ok=True)
    if args.command == "status":
        service = runtime()
        record = service.load(RECORD)
        attempted = {state["episode_id"] for state in service.load(STATUS).get("jobs", [])
                     if state["state"] in ("failed", "running")}
        print(json.dumps({"host": args.host, "worker_alive": bool(service.alive(record)),
                          "worker": service.load(STATUS), "jobs": inspect_jobs(args.host, attempted=attempted)}))
    elif args.command == "start":
        print(json.dumps(start(args.host, limit)))
    else:
        serve(args.host, limit)


if __name__ == "__main__":
    main()
