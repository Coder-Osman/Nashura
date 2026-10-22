import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--checkpoint-sha", required=True)
    parser.add_argument("--engine-sha", required=True)
    args = parser.parse_args()
    root, parent, base = args.root, args.parent, args.base
    project = parent / "project"
    sys.path.insert(0, str(project))
    from native_runner.training.fair_final.episode import atomic_json
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.evaluate_policy import wait_for_engine
    from tools.fair_migration_supplement import validate_supplement_round

    deployed = json.loads((root / "deployed-source.json").read_text())
    for relative, expected in deployed.items():
        if hashlib.sha256((project / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("supplementary deployed source identity changed")
    engine_path = base / "project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so"
    if hashlib.sha256(engine_path.read_bytes()).hexdigest() != args.engine_sha:
        raise ValueError("supplementary native engine identity changed")
    if hashlib.sha256((parent / "engineering-migration-update.pt").read_bytes()).hexdigest() != args.checkpoint_sha:
        raise ValueError("supplementary engineering checkpoint identity changed")
    parent_manifest = json.loads((parent / "manifest.json").read_text())
    manifest = {**parent_manifest, "round": 1, "matches": {}}
    tasks = []
    for owner in (0, 1):
        match_id = semantic_digest([root.name, owner])
        models = [manifest["identity"]["source_a_sha"]] * 2
        models[owner] = args.checkpoint_sha
        manifest["matches"][match_id] = dict(pair_seed_id=semantic_digest([root.name, "paired"]),
            split="train", behavior_checkpoint_shas=models)
        tasks.append(dict(match_id=match_id, student_owner=owner, engine_seed=78113301,
            execution_seed=78113302))
    validate_supplement_round(manifest, parent_manifest, args.checkpoint_sha)
    atomic_json(root / "manifest.json", manifest)
    atomic_json(root / "frozen-tasks-private.json", tasks)
    environment = dict(os.environ, CR_PROBE=str(engine_path), PYTHONUNBUFFERED="1")
    reports, cleanups = [], []

    def interrupt(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    for index, task in enumerate(tasks):
        port = 29721
        instance = root / f"engine-{index}"
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
        child = engine = None
        with (root / f"engine-{index}.log").open("x") as log:
            try:
                engine = subprocess.Popen([sys.executable, str(base / "isolated_engine.py"), "--project",
                    str(base / "project"), "--port", str(port), "--instance", str(instance)],
                    stdout=log, stderr=subprocess.STDOUT)
                wait_for_engine(engine, instance, timeout=120)
                command = [sys.executable, str(project / "tools/fair_migration_supplement.py"),
                    "--port", str(port), "--output", str(root / "data"),
                    "--manifest", str(root / "manifest.json"), "--parent-data", str(parent / "data"),
                    "--parent-manifest", str(parent / "manifest.json"), "--source", str(base / "candidate.pt"),
                    "--student", str(parent / "engineering-migration-update.pt"),
                    "--student-sha256", args.checkpoint_sha, "--engine-sha256", args.engine_sha,
                    "--match-id", task["match_id"], "--engine-seed", str(task["engine_seed"]),
                    "--execution-seed", str(task["execution_seed"]), "--device", "cuda:0",
                    "--wall-seconds", "1200", "--engineering-acceptance"]
                started = time.monotonic()
                with (root / f"collector-{index}.log").open("x") as output:
                    child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, env=environment)
                    while child.poll() is None:
                        if time.monotonic() - started > 1320:
                            raise TimeoutError("bounded supplementary collector exceeded limit")
                        progress = root / "data/progress" / f"{task['match_id']}-1.json"
                        state = json.loads(progress.read_text()) if progress.exists() else dict(status="starting")
                        state.update(student_owner=task["student_owner"], completed=len(reports))
                        atomic_json(root / "status.json", state)
                        time.sleep(5)
                if child.returncode:
                    raise RuntimeError(f"supplementary native collector {index} failed: {child.returncode}")
                reports.append(json.loads((root / "data/attempts" / task["match_id"] / "1/report.json").read_text()))
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                if engine is not None:
                    engine.terminate()
                    try:
                        engine.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        engine.kill()
                        engine.wait()
                    state = json.loads((instance / "process.json").read_text())
                    if state["phase"] != "stopped":
                        raise ValueError("owned supplementary engine did not cleanly stop")
                    for pid in (state["pid"], state.get("child_pid")):
                        if pid and Path(f"/proc/{pid}").exists():
                            raise ValueError("owned supplementary engine process still exists")
                    with socket.socket() as check:
                        check.bind(("127.0.0.1", port))
                    cleanups.append(dict(instance=instance.name, **state, port_released=True))
                    atomic_json(root / "engine-cleanup.json", cleanups)
    atomic_json(root / "report.json", dict(schema="fair-final-native-supplementary-probe.v1",
        status="engineering_completed", reports=reports, engine_cleanup=cleanups,
        source_sha256=deployed, engine_sha256=args.engine_sha, training_admissible=False,
        formal_g2_pass=False, round=1, complete_seat_swapped_pairs=1))
    atomic_json(root / "status.json", dict(status="engineering_completed", completed=2,
        rows=sum(report["rows"] for report in reports), training_admissible=False))


if __name__ == "__main__":
    main()
