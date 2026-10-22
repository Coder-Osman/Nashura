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

import torch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--engine-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    root, base = args.root, args.base
    project = root / "project"
    sys.path.insert(0, str(project))
    from native_runner.training.fair_final.adapter import ADAPTER_ID
    from native_runner.training.fair_final.episode import atomic_json
    from native_runner.training.fair_migration_collect import collection_protocols
    from native_runner.training.fair_migration_data import DATASET_SCHEMA, MigrationDataset, MigrationStore
    from native_runner.training.fair_search.config import HOG_DECK
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.evaluate_policy import wait_for_engine
    from native_runner.training.fair_search.model import load_checkpoint

    torch.set_num_threads(1)
    source_manifest = json.loads((project / "source-manifest.json").read_text())
    for relative, expected in source_manifest.items():
        if hashlib.sha256((project / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("worker deployed source identity changed")
    engine_path = base / "project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so"
    if hashlib.sha256(engine_path.read_bytes()).hexdigest() != args.engine_sha:
        raise ValueError("worker native engine identity changed")
    teacher, _, source_sha = load_checkpoint(base / "candidate.pt", device="cpu", expected_sha256=args.source_sha)
    teacher.eval().requires_grad_(False)
    identity = dict(source_a_sha=source_sha, source_network_sha=semantic_digest(teacher.state_dict()),
        migration_adapter_sha=semantic_digest(ADAPTER_ID), **collection_protocols(args.engine_sha))
    tasks = []
    for index, split in enumerate(("train", "validation", "test")):
        tasks.append(dict(match_id=semantic_digest([root.name, split]),
            pair_seed_id=semantic_digest([root.name, "pair", split]), split=split,
            engine_seed=78112101 + index, execution_seed=78112201 + index))
    manifest = dict(schema=DATASET_SCHEMA, namespace_sha=semantic_digest([root.name, "engineering-only"]),
        identity=identity, card_ids=list(HOG_DECK), round=0,
        matches={task["match_id"]: dict(pair_seed_id=task["pair_seed_id"], split=task["split"],
            behavior_checkpoint_shas=[source_sha, source_sha]) for task in tasks})
    store = MigrationStore(root / "data", manifest=manifest)
    atomic_json(root / "frozen-tasks-private.json", tasks)
    atomic_json(root / "manifest.json", manifest)
    environment = dict(os.environ, CR_PROBE=str(engine_path), PYTHONUNBUFFERED="1")
    engines, reports = [], []

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
        with (root / f"engine-{index}.log").open("w") as log:
            try:
                engine = subprocess.Popen([sys.executable, str(base / "isolated_engine.py"), "--project",
                    str(base / "project"), "--port", str(port), "--instance", str(instance)],
                    stdout=log, stderr=subprocess.STDOUT)
                wait_for_engine(engine, instance, timeout=120)
                command = [sys.executable, str(project / "tools/collect_fair_final_migration.py"),
                    "--port", str(port), "--output", str(store.root), "--manifest", str(root / "manifest.json"),
                    "--source", str(base / "candidate.pt"), "--source-manifest", str(project / "source-manifest.json"),
                    "--engine-sha256", args.engine_sha, "--match-id", task["match_id"],
                    "--engine-seed", str(task["engine_seed"]), "--execution-seed", str(task["execution_seed"]),
                    "--wall-seconds", "1200", "--engineering-acceptance"]
                started = time.monotonic()
                with (root / f"collector-{index}.log").open("w") as output:
                    child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, env=environment)
                    while child.poll() is None:
                        if time.monotonic() - started > 1320:
                            raise TimeoutError("bounded native migration collector exceeded limit")
                        progress = store.root / "progress" / f"{task['match_id']}-1.json"
                        state = json.loads(progress.read_text()) if progress.exists() else dict(status="starting")
                        state.update(split=task["split"], completed=len(reports))
                        print(json.dumps(state), flush=True)
                        atomic_json(root / "status.json", state)
                        time.sleep(5)
                if child.returncode:
                    raise RuntimeError(f"native migration collector {index} failed: {child.returncode}")
                report_path = store.root / "attempts" / task["match_id"] / "1/report.json"
                reports.append(json.loads(report_path.read_text()))
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
                        raise ValueError("owned migration engine did not cleanly stop")
                    for pid in (state["pid"], state.get("child_pid")):
                        if pid and Path(f"/proc/{pid}").exists():
                            raise ValueError("owned migration engine process still exists")
                    with socket.socket() as check:
                        check.bind(("127.0.0.1", port))
                    engines.append(dict(instance=instance.name, **state, port_released=True))
                    atomic_json(root / "engine-cleanup.json", engines)
    dataset = MigrationDataset(store, teacher=teacher)
    checks = {}
    for split, row_ids in dataset.splits.items():
        frame, legalities = dataset.batch([row_ids[0], row_ids[-1]], split=split)
        checks[split] = dict(rows=len(row_ids), checked_rows=2, frame_batch=frame.batch_size,
            legalities=len(legalities))
    report = dict(schema="fair-final-native-migration-probe.v1", status="passed", reports=reports,
        engine_cleanup=engines, dataset_identity=dataset.identity, splits=checks,
        source_sha256=source_manifest, engine_sha256=args.engine_sha, training_admissible=False)
    atomic_json(root / "native-migration-report.json", report)
    atomic_json(root / "status.json", dict(status="passed", completed=3, rows=len(dataset.index)))
    print(json.dumps(dict(status="passed", completed=3, rows=len(dataset.index))), flush=True)


if __name__ == "__main__":
    main()
