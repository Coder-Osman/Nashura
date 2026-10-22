"""Deploy and aggregate the remote formal teacher/PPO collection queues."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import time

from fleet_deploy import connect, inventory, run
from formal_contract import HOST_ORDER, ppo_jobs, teacher_jobs

ROOT = Path(__file__).resolve().parent
FILES = ("formal_contract.py", "formal_host_worker.py", "event_puct_acceptance.py", "collect_policy_episode.py",
         "distillation.py", "teacher_targets.py", "event_policy_rollout.py", "runtime_contract.py")


def sync(client, task):
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}
    command = "for filename in " + " ".join(shlex.quote(f"{task}/{name}") for name in FILES)
    command += '; do if [ -f "$filename" ]; then sha256sum "$filename"; fi; done'
    output = run(client, command)
    actual = {Path(path).name: digest for digest, path in (line.split(maxsplit=1) for line in output.splitlines())}
    changed = [name for name in FILES if actual.get(name) != expected[name]]
    with client.open_sftp() as sftp:
        for name in changed:
            temporary = f"{task}/{name}.deploying"
            sftp.put(str(ROOT / name), temporary)
            sftp.posix_rename(temporary, f"{task}/{name}")
    output = run(client, "sha256sum " + " ".join(shlex.quote(f"{task}/{name}") for name in FILES))
    actual = {Path(path).name: digest for digest, path in (line.split(maxsplit=1) for line in output.splitlines())}
    if actual != expected:
        raise ValueError("formal collector hash verification failed")
    return changed


def paths(phase, cycle, update):
    return teacher_jobs(ROOT, cycle) if phase == "teacher" else ppo_jobs(ROOT, update)


def prepare_opponents(inventory_path):
    credentials = inventory(inventory_path)
    config = json.loads((ROOT / "config.json").read_text())
    weights = tuple(value["sha256"] for value in config["opponent_checkpoints"].values())
    sources = {sha: ROOT.parent / "render-web" / "weights" / f"{sha}.pt" for sha in weights}
    for sha, source in sources.items():
        if not source.is_file():
            raise ValueError(f"frozen opponent checkpoint differs from pinned SHA: {sha}")
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                raise ValueError(f"frozen opponent checkpoint differs from pinned SHA: {sha}")
    def one(host):
        client = connect(host, credentials[host])
        root = f"/home/{credentials[host][0]}/firstlight_CR"
        directory = f"{root}/artifacts/render-web/weights"
        try:
            run(client, f"mkdir -p {shlex.quote(directory)}")
            copied = []
            with client.open_sftp() as sftp:
                for sha, source in sources.items():
                    target = f"{directory}/{sha}.pt"
                    existing = run(client, f"if [ -f {shlex.quote(target)} ]; then sha256sum {shlex.quote(target)}; fi")
                    if existing:
                        if existing.split()[0] != sha:
                            raise ValueError(f"existing frozen checkpoint has a different SHA: {sha}")
                        continue
                    temporary = target + ".deploying"
                    sftp.put(str(source), temporary)
                    sftp.posix_rename(temporary, target)
                    copied.append(sha)
            output = run(client, "sha256sum " + " ".join(shlex.quote(f"{directory}/{sha}.pt") for sha in weights))
            actual = {Path(path).stem: digest for digest, path in (line.split(maxsplit=1) for line in output.splitlines())}
            if actual != {sha: sha for sha in weights}:
                raise ValueError("frozen opponent checkpoint hash verification failed")
            return {"host": host, "copied": copied, "verified": True}
        except Exception as error:
            return {"host": host, "error": str(error)}
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=5) as pool:
        hosts = list(pool.map(one, HOST_ORDER))
    return {"prepared_at": time.time(), "hosts": hosts,
            "passed": all(item.get("verified") is True for item in hosts)}


def launch(phase, cycle, update, inventory_path):
    if phase == "ppo":
        teachers = status("teacher", cycle, update, inventory_path)
        if not teachers["passed"]:
            raise ValueError("formal PPO collection requires 216 verified teacher episodes first")
        opponents = prepare_opponents(inventory_path)
        if not opponents["passed"]:
            raise ValueError("frozen opponent checkpoints are not verified on all five hosts")
    credentials = inventory(inventory_path)
    planned = paths(phase, cycle, update)
    def one(host):
        client = connect(host, credentials[host])
        root = f"/home/{credentials[host][0]}/firstlight_CR"
        task = f"{root}/artifacts/{ROOT.name}"
        try:
            changed = sync(client, task)
            output = run(client, f"cd {shlex.quote(root)} && .venv/bin/python {shlex.quote(task + '/formal_host_worker.py')} start "
                                f"--host {host} --phase {phase} --cycle {cycle} --update {update}")
            return {"host": host, "synchronized_files": changed, "worker": json.loads(output)}
        except Exception as error:
            return {"host": host, "error": str(error)}
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(one, HOST_ORDER))
    return {"launched_at": time.time(), "phase": phase, "cycle": cycle, "update": update,
            "planned_jobs": len(planned), "hosts": results,
            "dispatched": all("error" not in item for item in results)}


def status(phase, cycle, update, inventory_path):
    credentials = inventory(inventory_path)
    planned = paths(phase, cycle, update)
    grouped = {host: [job for job in planned if job["host"] == host] for host in HOST_ORDER}
    def one(host):
        client = connect(host, credentials[host])
        root = f"/home/{credentials[host][0]}/firstlight_CR"
        task = f"{root}/artifacts/{ROOT.name}"
        try:
            output = run(client, f"cd {shlex.quote(root)} && .venv/bin/python {shlex.quote(task + '/formal_host_worker.py')} status "
                                f"--host {host} --phase {phase} --cycle {cycle} --update {update}")
            return json.loads(output)
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=5) as pool:
        reports = list(pool.map(one, HOST_ORDER))
    states = [state for report in reports for state in report["jobs"]]
    counts = {name: sum(state["state"] == name for state in states)
              for name in ("passed", "running", "pending", "failed", "invalid")}
    return {"schema": "hog-formal-collection.v1", "checked_at": time.time(), "phase": phase,
            "cycle": cycle, "update": update, "jobs": states, "counts": counts,
            "passed": counts["passed"] == len(planned) and not counts["running"] and not counts["pending"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("launch", "status", "prepare-opponents"))
    parser.add_argument("--phase", choices=("teacher", "ppo"))
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--update", type=int, default=1)
    parser.add_argument("--inventory", type=Path, default=ROOT.parents[2] / "MACHINES.md")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare-opponents":
        result = prepare_opponents(args.inventory)
    else:
        if args.phase is None:
            parser.error("--phase is required for launch and status")
        result = (launch if args.command == "launch" else status)(args.phase, args.cycle, args.update, args.inventory)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("phase", "cycle", "update", "planned_jobs", "counts", "passed", "dispatched", "hosts") if key in result}, indent=2))
    if args.command == "launch" and not result["dispatched"]:
        raise SystemExit(1)
    if args.command == "prepare-opponents" and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
