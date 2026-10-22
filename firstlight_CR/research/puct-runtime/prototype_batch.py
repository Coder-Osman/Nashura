"""Synchronize collectors, resume bounded host queues, and aggregate prototype evidence."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import time

from fleet_deploy import connect, inventory, run
from prototype_contract import HOST_ORDER, jobs

ROOT = Path(__file__).resolve().parent
COLLECTOR_FILES = (
    "config.json", "match-plan.json", "event_puct_acceptance.py", "event_policy_rollout.py",
    "distillation.py", "policy_signature.py", "tree_search.py", "native_adapter.py", "policy_rollout.py",
    "teacher_targets.py", "runtime_contract.py", "prototype_contract.py", "prototype_host_worker.py",
)


def load_config():
    return json.loads((ROOT / "config.json").read_text())


def _remote_root(credentials, host):
    return f"/home/{credentials[host][0]}/firstlight_CR"


def sync_collector(client, task):
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in COLLECTOR_FILES}
    command = "for filename in " + " ".join(shlex.quote(f"{task}/{name}") for name in COLLECTOR_FILES)
    command += '; do if [ -f "$filename" ]; then sha256sum "$filename"; fi; done'
    output = run(client, command)
    actual = {Path(path).name: digest for digest, path in (line.split(maxsplit=1) for line in output.splitlines())}
    changed = [name for name in COLLECTOR_FILES if actual.get(name) != expected[name]]
    with client.open_sftp() as sftp:
        for name in changed:
            target = f"{task}/{name}"
            sftp.put(str(ROOT / name), target + ".deploying")
            sftp.posix_rename(target + ".deploying", target)
    output = run(client, "sha256sum " + " ".join(shlex.quote(f"{task}/{name}") for name in COLLECTOR_FILES))
    actual = {Path(path).name: digest for digest, path in (line.split(maxsplit=1) for line in output.splitlines())}
    if actual != expected:
        raise ValueError("prototype collector hash verification failed")
    return changed


def _launch_host(host, host_jobs, credentials):
    client = connect(host, credentials[host])
    root = _remote_root(credentials, host)
    task = f"{root}/artifacts/{ROOT.name}"
    try:
        changed = sync_collector(client, task)
        output = run(client, f"cd {shlex.quote(root)} && .venv/bin/python "
                     f"{shlex.quote(task + '/event_puct_acceptance.py')} --help")
        if "--episode-id" not in output or "--output" not in output:
            raise ValueError("prototype collector CLI version mismatch")
        output = run(client, f"cd {shlex.quote(root)} && .venv/bin/python "
                     f"{shlex.quote(task + '/prototype_host_worker.py')} start --host {host}")
        return {"host": host, "planned": len(host_jobs), "synchronized_files": changed,
                "worker": json.loads(output)}
    finally:
        client.close()


def launch(inventory_path):
    credentials = inventory(inventory_path)
    planned = jobs(load_config())
    def launch_host(host):
        try:
            return _launch_host(host, [job for job in planned if job["host"] == host], credentials)
        except Exception as error:
            return {"host": host, "error": str(error)}
    with ThreadPoolExecutor(max_workers=5) as pool:
        hosts = list(pool.map(launch_host, HOST_ORDER))
    return {"launched_at": time.time(), "planned_jobs": len(planned), "hosts": hosts,
            "dispatched": all("error" not in host for host in hosts)}


def _status_host(host, host_jobs, credentials):
    client = connect(host, credentials[host])
    root = _remote_root(credentials, host)
    try:
        task = f"{root}/artifacts/{ROOT.name}"
        output = run(client, f"cd {shlex.quote(root)} && .venv/bin/python "
                     f"{shlex.quote(task + '/prototype_host_worker.py')} status --host {host}")
        report = json.loads(output)
        return [{**state, "worker_alive": report["worker_alive"]} for state in report["jobs"]]
    finally:
        client.close()


def collect(inventory_path):
    credentials = inventory(inventory_path)
    planned = jobs(load_config())
    grouped = {host: [job for job in planned if job["host"] == host] for host in HOST_ORDER}
    def inspect_host(host):
        try:
            states = _status_host(host, grouped[host], credentials)
            expected = {job["episode_id"] for job in grouped[host]}
            if len(states) != len(expected) or {state["episode_id"] for state in states} != expected:
                raise ValueError("host result job allocation mismatch")
            return states
        except Exception as error:
            return [{**job, "state": "unreachable", "verified": False, "error": str(error)}
                    for job in grouped[host]]
    with ThreadPoolExecutor(max_workers=5) as pool:
        states = [state for chunk in pool.map(inspect_host, HOST_ORDER) for state in chunk]
    passed = [state for state in states if state.get("verified") is True and state["state"] == "passed"]
    return {"schema": "hog-puct-prototype-acceptance.v1", "checked_at": time.time(),
            "passed": len(passed) == 108, "games": len(passed), "required_games": 108, "simulations": 32,
            "jobs": states, "failures": [state for state in states if state not in passed],
            "state_counts": {name: sum(state["state"] == name for state in states)
                             for name in ("passed", "running", "pending", "failed", "invalid", "conflict", "unreachable")},
            "host_counts": {host: sum(state["host"] == host for state in passed) for host in HOST_ORDER}}


def write_summary(summary, inventory_path, output):
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, indent=2) + "\n")
    temporary.replace(output)
    if not summary["passed"]:
        return
    credentials = inventory(inventory_path)
    client = connect("37", credentials["37"])
    try:
        target = f"{_remote_root(credentials, '37')}/artifacts/{ROOT.name}/prototype_acceptance.json"
        with client.open_sftp() as sftp:
            with sftp.open(target + ".tmp", "w") as stream:
                stream.write(json.dumps(summary, indent=2) + "\n")
            sftp.posix_rename(target + ".tmp", target)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "launch", "status"))
    parser.add_argument("--inventory", type=Path, default=ROOT.parents[2] / "MACHINES.md")
    parser.add_argument("--output", type=Path, default=ROOT / "prototype_acceptance.json")
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps({"jobs": jobs(load_config())}, indent=2))
    elif args.command == "launch":
        result = launch(args.inventory)
        print(json.dumps(result, indent=2))
        if not result["dispatched"]:
            raise SystemExit(1)
    else:
        summary = collect(args.inventory)
        write_summary(summary, args.inventory, args.output)
        print(json.dumps({key: summary[key] for key in ("passed", "games", "required_games", "simulations",
                                                       "state_counts", "host_counts")}, indent=2))
        if not summary["passed"]:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
