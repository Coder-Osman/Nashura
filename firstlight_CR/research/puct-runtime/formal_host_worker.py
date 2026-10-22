"""Task-owned remote worker for one formal teacher or PPO collection queue."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import os
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "runtime"), str(ROOT.parents[1])]

from formal_contract import CONCURRENCY, ppo_jobs, teacher_jobs
from server_engine import owned_alive, resources, runtime


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def args_for(pid):
    try:
        return [os.fsdecode(value) for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if value]
    except OSError:
        return []


def matches(arguments, job, phase):
    script = "event_puct_acceptance.py" if phase == "teacher" else "collect_policy_episode.py"
    if len(arguments) < 2 or Path(arguments[1]).resolve() != ROOT / script:
        return False
    required = {"port": job["port"], "seed": job["seed"]}
    for name, value in required.items():
        option = "--" + name
        try:
            if arguments[arguments.index(option) + 1] != str(value):
                return False
        except (ValueError, IndexError):
            return False
    return True


def processes(jobs, phase):
    found = {}
    for entry in Path("/proc").glob("[0-9]*/cmdline"):
        pid = int(entry.parent.name)
        arguments = args_for(pid)
        for job in jobs:
            if matches(arguments, job, phase):
                found.setdefault(job["episode_id"], []).append(pid)
    return found


def validate(job, phase):
    config = json.loads((ROOT / "config.json").read_text())
    if phase == "teacher":
        path = ROOT / job["output"]
        if not path.exists():
            return False, None
        try:
            from distillation import TeacherRecord
            result = json.loads(path.read_text())
            record = TeacherRecord.from_dict(result["teacher_record"])
            record.require_training_ready()
            if (result.get("passed") is not True or result.get("simulations") != job["simulations"] or
                    result.get("episode_id") != job["episode_id"] or result.get("seed") != job["seed"] or
                    result.get("port") != job["port"] or result.get("device") != job["device"] or
                    result.get("checkpoint_sha256") != config["start_checkpoint_sha256"] or
                    result.get("root_fingerprint") != result.get("restored_root_fingerprint") or
                    result.get("snapshot_handles_after") != 0 or
                    result.get("episode") != record.terminal_evidence or
                    record.episode_id != job["episode_id"] or record.seed != job["seed"] or
                    record.search_simulations != job["simulations"]):
                raise ValueError("teacher identity or simulation budget mismatch")
            execution = result.get("execution", {})
            if (execution.get("rejected") != 0 or execution.get("unconfirmed") != 0 or
                    execution.get("executed") != execution.get("confirmed") or
                    execution.get("submitted") != execution.get("confirmed", 0) +
                    execution.get("cancelled_native_terminal", 0)):
                raise ValueError("teacher execution ledger is not clean")
            return True, {"state": "passed", "verified": True, "simulations": job["simulations"]}
        except Exception as error:
            return False, {"state": "invalid", "error": str(error)}
    directory = ROOT / job["destination"]
    summary = directory / "part-00000.json"
    episode = directory / "episode.json"
    if not (summary.exists() and (directory / "part-00000.crev.zst").exists() and episode.exists()):
        return False, None
    try:
        from native_runner.training.event_cache import load_event_rl_shard
        value = json.loads(episode.read_text())
        shard = load_event_rl_shard(summary, verify=True)
        expected_opponent = (config["start_checkpoint_sha256"] if job["opponent_kind"] == "self_play" else
                             config["opponent_checkpoints"][job["opponent_kind"]]["sha256"])
        contract = shard.manifest["contract"]
        if (value.get("passed") is not True or value.get("opponent_kind") != job["opponent_kind"] or
                value.get("seed") != job["seed"] or value.get("port") != job["port"] or
                value.get("checkpoint_sha256") != config["start_checkpoint_sha256"] or
                value.get("opponent_sha256") != expected_opponent or
                value.get("episode", {}).get("ended") is not True or
                value.get("episode", {}).get("truncated") is not False or
                contract.get("collection_mode") != "on-policy" or
                contract.get("search_enabled") is not False or
                contract.get("opponent_kind") != job["opponent_kind"] or
                contract.get("opponent_sha256") != expected_opponent or not shard.records or
                not any(row.done for row in shard.records)):
            raise ValueError("PPO episode is incomplete or allocation differs")
        return True, {"state": "passed", "verified": True, "rows": len(shard.records)}
    except Exception as error:
        return False, {"state": "invalid", "error": str(error)}


def inspect(jobs, phase, attempted):
    running = processes(jobs, phase)
    states = []
    for job in jobs:
        state = dict(job, state="pending", verified=False, pids=running.get(job["episode_id"], []))
        passed, evidence = validate(job, phase)
        if passed:
            state.update(evidence)
        elif evidence:
            state.update(evidence)
        elif state["pids"]:
            state["state"] = "running"
        elif job["episode_id"] in attempted:
            state.update(state="failed", error="collector exited without a verified result")
        states.append(state)
    return states


def attempted_jobs(status_path):
    if not status_path.exists():
        return set()
    previous = json.loads(status_path.read_text())
    return {state["episode_id"] for state in previous.get("jobs", [])
            if state["state"] in ("failed", "running")}


def available_slots(states, limit):
    slots = max(0, limit - sum(len(state["pids"]) for state in states))
    try:
        reserve = resources()
    except RuntimeError as error:
        return 0, str(error)
    return min(slots, max(0, int((reserve["memory_available_gib"] - 32) // 2.5))), None


def start_job(job, phase, service):
    record = service.load(ROOT / "engines" / str(job["port"]) / "process.json")
    if not record.get("ready") or not owned_alive(service, record, job["port"]):
        raise RuntimeError(f"task-owned engine {job['port']} is not ready")
    status = service.request(job["port"], "status")
    if status.get("ok") is not True or status.get("coldReady") is not True or status.get("contentReady") is not True or status.get("snapshotHandles") != 0:
        raise RuntimeError(f"native engine {job['port']} is not clean and ready")
    if phase == "teacher":
        command = [sys.executable, str(ROOT / "event_puct_acceptance.py"), "--port", str(job["port"]),
                   "--device", job["device"], "--seed", str(job["seed"]), "--simulations", str(job["simulations"]),
                   "--episode-id", job["episode_id"], "--output", str(ROOT / job["output"])]
    else:
        command = [sys.executable, str(ROOT / "collect_policy_episode.py"), "--port", str(job["port"]),
                   "--device", job["device"], "--seed", str(job["seed"]), "--opponent-kind", job["opponent_kind"],
                   "--destination", str(ROOT / job["destination"])]
    log = ROOT / job["log"]
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stream:
        stream.write(json.dumps({"event": "formal-attempt", "started_at": time.time(), "job": job}) + "\n")
        stream.flush()
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                 start_new_session=True)
    pid = ROOT / job["pid"]
    pid.parent.mkdir(parents=True, exist_ok=True)
    pid.write_text(str(child.pid) + "\n")
    return child


def serve(host, phase, cycle, update, limit):
    service = runtime()
    jobs = teacher_jobs(ROOT, cycle) if phase == "teacher" else ppo_jobs(ROOT, update)
    jobs = [job for job in jobs if job["host"] == host]
    batch = ROOT / (f"formal/teachers/cycle-{cycle:03d}" if phase == "teacher" else f"formal/ppo/update-{update:03d}")
    status_path = batch / f"worker-status-{host}.json"
    record_path = batch / f"worker-process-{host}.json"
    attempted, children = attempted_jobs(status_path), {}
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (batch / f"worker-{host}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save(record_path, {"pid": os.getpid(), "host": host, "phase": phase, "cycle": cycle, "update": update})
        while not stopping:
            for child in children.values():
                child.poll()
            states = inspect(jobs, phase, attempted)
            attempted.update(state["episode_id"] for state in states if state["state"] == "running")
            slots, resource_error = available_slots(states, limit)
            for state in states:
                if slots <= 0 or state["state"] != "pending":
                    continue
                current_slots, resource_error = available_slots(states, limit)
                if current_slots < 1:
                    break
                try:
                    if (ROOT / "STOP").exists() or (ROOT / "BOUNDARY_STOP").exists():
                        raise RuntimeError("task stop marker is present")
                    child = start_job(state, phase, service)
                    children[state["episode_id"]] = child
                    attempted.add(state["episode_id"])
                    slots -= 1
                    state.update(state="running", pids=[child.pid])
                    time.sleep(2)
                except Exception as error:
                    attempted.add(state["episode_id"])
                    state.update(state="failed", error=str(error))
            counts = {name: sum(state["state"] == name for state in states)
                      for name in ("passed", "running", "pending", "failed", "invalid")}
            save(status_path, {"host": host, "phase": phase, "cycle": cycle, "update": update,
                               "checked_at": time.time(), "counts": counts,
                               "resource_error": resource_error, "jobs": states})
            if not counts["pending"] and not counts["running"]:
                break
            time.sleep(15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "status", "_serve"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--phase", choices=("teacher", "ppo"), required=True)
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--update", type=int, default=1)
    parser.add_argument("--max-concurrency", type=int)
    args = parser.parse_args()
    limit = CONCURRENCY[args.host] if args.max_concurrency is None else args.max_concurrency
    if not 1 <= limit <= CONCURRENCY[args.host]:
        parser.error("concurrency exceeds the host limit")
    batch = ROOT / (f"formal/teachers/cycle-{args.cycle:03d}" if args.phase == "teacher" else f"formal/ppo/update-{args.update:03d}")
    batch.mkdir(parents=True, exist_ok=True)
    record_path = batch / f"worker-process-{args.host}.json"
    if args.command == "status":
        jobs = teacher_jobs(ROOT, args.cycle) if args.phase == "teacher" else ppo_jobs(ROOT, args.update)
        jobs = [job for job in jobs if job["host"] == args.host]
        record = json.loads(record_path.read_text()) if record_path.exists() else {}
        status_path = batch / f"worker-status-{args.host}.json"
        attempted = attempted_jobs(status_path)
        print(json.dumps({"worker_alive": bool(record.get("pid")) and Path(f"/proc/{record['pid']}").exists(),
                          "jobs": inspect(jobs, args.phase, attempted)}))
    elif args.command == "start":
        record = json.loads(record_path.read_text()) if record_path.exists() else {}
        if record.get("pid") and Path(f"/proc/{record['pid']}").exists():
            print(json.dumps({"already_running": True, **record}))
            return
        with (batch / f"worker-{args.host}.log").open("a") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_serve", "--host", args.host,
                                      "--phase", args.phase, "--cycle", str(args.cycle), "--update", str(args.update),
                                      "--max-concurrency", str(limit)], cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        print(json.dumps({"already_running": False, "pid": child.pid, "host": args.host, "phase": args.phase}))
    else:
        serve(args.host, args.phase, args.cycle, args.update, limit)


if __name__ == "__main__":
    main()
