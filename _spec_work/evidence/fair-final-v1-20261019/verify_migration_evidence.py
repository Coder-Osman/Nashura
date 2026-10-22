import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys

import torch

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2] / "firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_migration_collect import collection_protocols
from native_runner.training.fair_migration_data import MigrationStore, validate_manifest
from native_runner.training.fair_search.contracts import semantic_digest


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_migration_evidence(attempt, *, root=ROOT, source=SOURCE):
    directory = (root / attempt).resolve()
    if directory.parent != root.resolve():
        raise ValueError("migration evidence must be an independent attempt")
    launch = read(directory / "launch.json")
    completion = read(directory / "completion.json")
    finalization = read(directory / "finalization.json")
    evidence = directory / "evidence"
    if (completion != dict(exit_code=0, remote=launch["remote"])
            or finalization["status"] != "passed" or finalization["training_admissible"] is not False
            or sha(directory / "evidence.tar.gz") != finalization["archive"]["sha256"]
            or (directory / "evidence.tar.gz").stat().st_size != finalization["archive"]["bytes"]):
        raise ValueError("migration completion or downloaded archive identity differs")
    if (sha(evidence / "worker.py") != launch["worker_sha256"]
            or sha(root / "migration_probe_worker.py") != launch["worker_sha256"]
            or sha(evidence / "training-probe.py") != finalization["training_probe_sha256"]
            or sha(root / "migration_native_training_probe.py") != finalization["training_probe_sha256"]):
        raise ValueError("migration worker or training probe identity differs")
    identities = read(directory / "source-manifest.json")
    required = {str(path.relative_to(source)) for path in (source / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(source).parts}
    required.add("tools/collect_fair_final_migration.py")
    if (set(identities) != required or read(evidence / "project/source-manifest.json") != identities
            or any(sha(source / relative) != expected for relative, expected in identities.items())):
        raise ValueError("native migration deployed source is stale or incomplete")
    manifest = read(evidence / "manifest.json")
    validate_manifest(manifest)
    if (launch["source_checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
            or launch["engine_sha256"] != "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
            or manifest["identity"]["source_a_sha"] != launch["source_checkpoint_sha256"]
            or manifest["round"] != 0 or len(manifest["matches"]) != 3
            or Counter(task["split"] for task in manifest["matches"].values()) != dict(train=1, validation=1, test=1)
            or any(manifest["identity"][key] != value
                for key, value in collection_protocols(launch["engine_sha256"]).items())):
        raise ValueError("migration source, engine, split or protocol differs")
    tasks = read(evidence / "frozen-tasks-private.json")
    expected_tasks = [dict(match_id=semantic_digest([attempt, split]),
        pair_seed_id=semantic_digest([attempt, "pair", split]), split=split,
        engine_seed=78112101 + index, execution_seed=78112201 + index)
        for index, split in enumerate(("train", "validation", "test"))]
    if tasks != expected_tasks or set(manifest["matches"]) != {task["match_id"] for task in tasks}:
        raise ValueError("migration preregistered tasks differ")
    store = MigrationStore(evidence / "data", manifest=manifest)
    if (len(list((store.root / "completed").glob("*.json"))) != 3
            or list((store.root / "attempts").rglob("quarantine.json"))):
        raise ValueError("migration completion coverage or quarantine differs")
    reports, cases, split_ids, receipts = [], [], {}, {}
    for task in tasks:
        match_id = task["match_id"]
        if manifest["matches"][match_id]["pair_seed_id"] != task["pair_seed_id"]:
            raise ValueError("migration frozen pair identity differs")
        episode, receipt = store.load(match_id)
        report = read(store.root / "attempts" / match_id / "1/report.json")
        raw = read(store.root / "audit" / match_id / "1/native-terminal.json")
        private = read(store.root / "audit" / match_id / "1/private-inputs.json")
        if (private["engine_seed"] != task["engine_seed"] or private["execution_seed"] != task["execution_seed"]
                or private["source_sha256"] != manifest["identity"]["source_a_sha"]
                or private["source_manifest_sha256"] != sha(evidence / "project/source-manifest.json")):
            raise ValueError("native migration attempt differs from frozen private audit")
        counts = {str(owner): sum(row["owner"] == owner for row in episode["rows"]) for owner in (0, 1)}
        lifecycle = dict(Counter(row["outcome"]["reason"] for row in episode["rows"]))
        if (report["schema"] != "fair-final-native-migration-episode.v1" or report["status"] != "passed"
                or report["training_admissible"] is not False or report["match_id"] != match_id
                or report["attempt_id"] != 1 or episode["attempt_id"] != 1
                or report["receipt"] != receipt or report["source_sha256"] != identities
                or report["source_network_sha"] != manifest["identity"]["source_network_sha"]
                or report["engine_sha256"] != launch["engine_sha256"]
                or report["rows"] != len(episode["rows"]) or report["rows_by_owner"] != counts
                or min(counts.values()) < 2 or report["lifecycle"] != lifecycle
                or report["terminal"] != episode["result"]
                or report["publication"] != dict(status="published", semantic_sha=receipt["semantic_sha"])
                or raw["tick"] != episode["result"]["terminal_tick"]
                or any(raw[key] != value for key, value in episode["result"]["native_evidence"].items())
                or not math.isfinite(report["seconds"]) or report["seconds"] <= 0
                or any(not math.isfinite(value) or value < 0 for value in report["timings"].values())):
            raise ValueError("native migration episode report differs from complete-game evidence")
        split_ids[task["split"]] = [row["row_id"] for row in episode["rows"]]
        receipts[match_id] = semantic_digest(receipt)
        reports.append(report)
        cases.append(dict(match_id=match_id, split=task["split"], rows=len(episode["rows"]),
            rows_by_owner=counts, lifecycle=lifecycle, terminal=episode["result"], seconds=report["seconds"],
            acted_rows=sum(row["behavior_action"]["card_id"] is not None for row in episode["rows"])))
    summary = read(evidence / "native-migration-report.json")
    expected_identity = dict(source_a_sha=manifest["identity"]["source_a_sha"],
        migration_adapter_sha=manifest["identity"]["migration_adapter_sha"],
        dataset_sha=semantic_digest(dict(manifest_sha=store.dataset_sha, receipts=receipts)))
    cleanups = read(evidence / "engine-cleanup.json")
    if (summary["schema"] != "fair-final-native-migration-probe.v1" or summary["status"] != "passed"
            or summary["training_admissible"] is not False or summary["reports"] != reports
            or summary["dataset_identity"] != expected_identity or summary["source_sha256"] != identities
            or summary["engine_sha256"] != launch["engine_sha256"] or len(cleanups) != 3
            or summary["engine_cleanup"] != cleanups):
        raise ValueError("native migration aggregate identity differs")
    for index, cleanup in enumerate(cleanups):
        process = read(evidence / f"engine-{index}/process.json")
        if (cleanup["instance"] != f"engine-{index}" or cleanup["phase"] != "stopped"
                or cleanup["port_released"] is not True
                or any(cleanup[key] != value for key, value in process.items())):
            raise ValueError("native migration engine cleanup evidence differs")
    expected_splits = {split: dict(rows=len(rows), checked_rows=2, frame_batch=2, legalities=2)
        for split, rows in split_ids.items()}
    if summary["splits"] != expected_splits:
        raise ValueError("migration native dataset split checks differ")
    update = read(evidence / "native-migration-update-report.json")
    checkpoint = evidence / "engineering-migration-update.pt"
    if sha(checkpoint) != update["checkpoint_sha256"]:
        raise ValueError("native migration update checkpoint identity differs")
    envelope = torch.load(checkpoint, weights_only=True, map_location="cpu")
    payload = envelope["payload"]
    if (update["schema"] != "fair-final-native-migration-update.v1" or update["status"] != "passed"
            or update["engineering_only"] is not True or update["training_admissible"] is not False
            or update["dataset_identity"] != expected_identity
            or update["split_rows"] != {key: len(value) for key, value in split_ids.items()}
            or update["batch_size"] != 1 or update["micro_batch_size"] != 1
            or any(update[key] is not True for key in ("teacher_unchanged", "encoder_unchanged", "exact_resume"))
            or update["expected"] != update["resumed"] or update["expected"]["steps"] != 2
            or update["expected"]["consumed"] != 2
            or update["before_network_sha"] == update["after_first_network_sha"]
            or update["after_first_network_sha"] == update["expected"]["network"]
            or semantic_digest(payload) != envelope["sha256"]
            or semantic_digest(payload["network"]) != update["after_first_network_sha"]
            or payload["identity"] != expected_identity
            or payload["teacher_network_sha"] != manifest["identity"]["source_network_sha"]
            or payload["cursor"]["optimizer_steps"] != 1 or payload["cursor"]["consumed"] != 1
            or payload["weights"] != update["weights"]
            or payload["weights"] != dict(conditional_policy_kl=1., greedy_nll=.1, wdl_teacher_kl=1.)
            or any(set(payload["splits"][key]) != set(value) for key, value in split_ids.items())
            or len(update["first_ids"]) != 1 or len(update["second_ids"]) != 1
            or update["first_ids"] == update["second_ids"]
            or not set(update["first_ids"] + update["second_ids"]).issubset(split_ids["train"])):
        raise ValueError("native migration update, split or exact-resume evidence differs")
    artifacts = [str(path.relative_to(root)) for path in directory.rglob("*") if path.is_file()]
    return dict(schema="fair-final-native-migration-evidence.v1", status="passed", attempt=attempt,
        cases=cases, rows=sum(case["rows"] for case in cases), dataset_identity=expected_identity,
        exact_resume=True, update_steps=2, engine_count=3, engine_cleanup="stopped", artifacts=artifacts,
        source_sha256=identities, training_admissible=False,
        scope="three complete native A-greedy engineering games and two batch-one updates; excludes formal migration, calibration and strength")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    report = validate_migration_evidence(args.attempt)
    atomic_json(ROOT / "native-migration-evidence-report.json", report)
    print(json.dumps({key: report[key] for key in ("status", "attempt", "rows", "exact_resume", "engine_count")}))


if __name__ == "__main__":
    main()
