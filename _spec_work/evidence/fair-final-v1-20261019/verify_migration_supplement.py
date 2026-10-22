import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2] / "firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_migration_collect import collection_protocols
from native_runner.training.fair_migration_data import MigrationStore
from native_runner.training.fair_search.contracts import semantic_digest
from tools.fair_migration_supplement import validate_supplement_round


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_supplement_evidence(directory, *, source=SOURCE, root=ROOT):
    directory = Path(directory)
    launch = read(directory / "launch.json")
    completion = read(directory / "completion.json")
    report = read(directory / "report.json")
    deployed = read(directory / "deployed-source.json")
    required = {str(path.relative_to(source)) for path in (source / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(source).parts}
    report_sources = required | {"tools/" + name for name in (
        "fair_migration_supplement.py", "diagnose_fair_final_migration.py", "fair_migration_diagnostics.py")}
    if (launch["engineering_only"] is not True or launch["training_admissible"] is not False
            or completion["exit_code"] != 0 or completion["training_admissible"] is not False
            or not report_sources.issubset(deployed)
            or any(sha(source / name) != expected for name, expected in deployed.items())
            or sha(directory / "worker.py") != launch["worker_sha256"]
            or sha(root / "migration_supplement_probe_worker.py") != launch["worker_sha256"]
            or any(deployed.get(name) != value for name, value in launch["source_sha256"].items())):
        raise ValueError("supplementary deployment or completion evidence differs")
    parent = root / "native-migration-1792548905478386000"
    manifest = read(directory / "manifest.json")
    validate_supplement_round(manifest, read(parent / "manifest.json"), launch["checkpoint_sha256"])
    if (manifest["round"] != 1 or len(manifest["matches"]) != 2
            or any(task["split"] != "train" for task in manifest["matches"].values())
            or any(manifest["identity"][key] != value for key, value
                in collection_protocols(launch["engine_sha256"]).items())):
        raise ValueError("supplementary engineering scope or protocol differs")
    parent_update = read(parent / "evidence/native-migration-update-report.json")
    if parent_update["checkpoint_sha256"] != launch["checkpoint_sha256"]:
        raise ValueError("supplementary parent checkpoint differs")
    expected_tasks = [dict(match_id=semantic_digest([launch["attempt"], owner]), student_owner=owner,
        engine_seed=78113301, execution_seed=78113302) for owner in (0, 1)]
    if (read(directory / "frozen-tasks-private.json") != expected_tasks
            or set(manifest["matches"]) != {task["match_id"] for task in expected_tasks}):
        raise ValueError("supplementary preregistered private tasks differ")
    store = MigrationStore(directory / "data", manifest=manifest)
    if (len(list((store.root / "completed").glob("*.json"))) != 2
            or list((store.root / "attempts").rglob("quarantine.json"))):
        raise ValueError("supplementary complete-game coverage differs")
    cases, reports = [], []
    expected_sources = {name: deployed[name] for name in report_sources}
    for task in expected_tasks:
        match_id = task["match_id"]
        episode, receipt = store.load(match_id)
        attempt = store.root / "attempts" / match_id / "1"
        current = read(attempt / "report.json")
        audit = store.root / "audit" / match_id / "1"
        private = read(audit / "private-inputs.json")
        terminal = read(audit / "native-terminal.json")
        counts = {str(owner): sum(row["owner"] == owner for row in episode["rows"]) for owner in (0, 1)}
        lifecycle = dict(Counter(row["outcome"]["reason"] for row in episode["rows"]))
        if (current["schema"] != "fair-final-supplementary-migration-episode.v1"
                or current["status"] != "passed" or current["training_admissible"] is not False
                or current["match_id"] != match_id or current["round"] != 1
                or current["attempt_id"] != 1 or episode["attempt_id"] != 1
                or current["student_owner"] != task["student_owner"]
                or current["student_checkpoint_sha"] != launch["checkpoint_sha256"]
                or current["student_network_sha"] != parent_update["after_first_network_sha"]
                or current["source_network_sha"] != manifest["identity"]["source_network_sha"]
                or current["decoding"] != "student-greedy-versus-A-adapter-greedy"
                or current["source_sha256"] != expected_sources
                or current["engine_sha256"] != launch["engine_sha256"]
                or current["rows"] != len(episode["rows"]) or current["rows_by_owner"] != counts
                or min(counts.values()) < 2 or current["lifecycle"] != lifecycle
                or current["terminal"] != episode["result"] or current["receipt"] != receipt
                or current["publication"] != dict(status="published", semantic_sha=receipt["semantic_sha"])
                or not math.isfinite(current["seconds"]) or current["seconds"] <= 0
                or any(not math.isfinite(value) or value < 0 for value in current["timings"].values())
                or terminal["tick"] != episode["result"]["terminal_tick"]
                or any(terminal[key] != value for key, value in episode["result"]["native_evidence"].items())
                or private["engine_seed"] != task["engine_seed"]
                or private["execution_seed"] != task["execution_seed"]
                or private["student_checkpoint_sha256"] != launch["checkpoint_sha256"]
                or private["student_network_sha"] != current["student_network_sha"]
                or private["source_sha256"] != manifest["identity"]["source_a_sha"]
                or private["parent_dataset_sha"] != parent_update["dataset_identity"]["dataset_sha"]
                or private["source_sha256_manifest"] != expected_sources):
            raise ValueError("supplementary report differs from complete-game evidence")
        reports.append(current)
        cases.append(dict(match_id=match_id, student_owner=task["student_owner"], rows=len(episode["rows"]),
            terminal=episode["result"], lifecycle=lifecycle, seconds=current["seconds"],
            behavior_teacher_disagreements=sum(row["behavior_action"] != row["teacher_action"]
                for row in episode["rows"])))
    cleanups = read(directory / "engine-cleanup.json")
    if len(cleanups) != 2:
        raise ValueError("supplementary cleanup coverage differs")
    for index, cleanup in enumerate(cleanups):
        process = read(directory / f"engine-{index}/process.json")
        if (cleanup["instance"] != f"engine-{index}" or cleanup["phase"] != "stopped"
                or cleanup["port_released"] is not True
                or any(cleanup[key] != value for key, value in process.items())):
            raise ValueError("supplementary owned-engine cleanup differs")
    if (report["schema"] != "fair-final-native-supplementary-probe.v1"
            or report["status"] != "engineering_completed" or report["reports"] != reports
            or report["source_sha256"] != deployed or report["engine_sha256"] != launch["engine_sha256"]
            or report["engine_cleanup"] != cleanups or report["round"] != 1
            or report["complete_seat_swapped_pairs"] != 1 or report["formal_g2_pass"] is not False
            or report["training_admissible"] is not False):
        raise ValueError("supplementary aggregate evidence differs")
    return dict(schema="fair-final-supplementary-evidence.v1", status="engineering_verified",
        attempt=launch["attempt"], cases=cases, rows=sum(case["rows"] for case in cases),
        round=1, complete_seat_swapped_pairs=1, training_admissible=False, formal_g2_pass=False,
        scope="round1 paired native collection only; excludes round2 native acceptance, formal quotas, training, calibration and strength")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = validate_supplement_evidence(args.directory)
    atomic_json(args.directory / "verification.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
