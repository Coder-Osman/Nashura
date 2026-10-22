import json
from pathlib import Path

import pytest

from verify_migration_evidence import ROOT, atomic_json, validate_migration_evidence


def current_attempt():
    return json.loads((ROOT / "native-migration-evidence-report.json").read_text())["attempt"]


def test_native_migration_evidence_accepts_complete_games_and_exact_resume():
    result = validate_migration_evidence(current_attempt())
    assert result["status"] == "passed" and result["engine_count"] == 3
    assert result["exact_resume"] is True and result["update_steps"] == 2
    assert {case["split"] for case in result["cases"]} == {"train", "validation", "test"}
    assert result["training_admissible"] is False


NEGATIVE_CASES = ["archive_sha", "source_manifest", "source_sha", "engine_sha", "worker_sha",
    "training_sha", "split", "pair", "seed", "receipt", "row_count", "owner_count", "lifecycle", "terminal_audit",
    "teacher_sha", "quarantine", "cleanup", "port", "dataset_identity", "split_check", "resume", "freeze",
    "update_steps", "checkpoint_sha", "train_ids", "weights", "completed_exit", "formal_admission"]


@pytest.mark.parametrize("mutation", NEGATIVE_CASES)
def test_native_migration_evidence_rejects_tampered_artifacts(tmp_path, mutation):
    attempt = current_attempt()
    original = ROOT / attempt
    target = tmp_path / attempt
    target.mkdir()
    for path in original.rglob("*"):
        destination = target / path.relative_to(original)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.symlink_to(path)
    for name in ("migration_probe_worker.py", "migration_native_training_probe.py"):
        (tmp_path / name).symlink_to(ROOT / name)
    evidence = target / "evidence"
    manifest = json.loads((evidence / "manifest.json").read_text())
    match = next(iter(manifest["matches"]))
    episode_report = evidence / "data/attempts" / match / "1/report.json"
    if mutation == "archive_sha":
        path, keys, value = target / "finalization.json", ["archive", "sha256"], "f" * 64
    elif mutation == "source_manifest":
        path, keys, value = target / "source-manifest.json", ["tools/collect_fair_final_migration.py"], "f" * 64
    elif mutation in ("source_sha", "engine_sha", "worker_sha"):
        key = {"source_sha": "source_checkpoint_sha256", "engine_sha": "engine_sha256", "worker_sha": "worker_sha256"}[mutation]
        path, keys, value = target / "launch.json", [key], "f" * 64
    elif mutation == "training_sha":
        path, keys, value = target / "finalization.json", ["training_probe_sha256"], "f" * 64
    elif mutation in ("split", "pair"):
        key = "split" if mutation == "split" else "pair_seed_id"
        changed_split = "test" if manifest["matches"][match]["split"] != "test" else "train"
        path, keys, value = evidence / "manifest.json", ["matches", match, key], changed_split if mutation == "split" else "f" * 64
    elif mutation == "seed":
        path, keys, value = evidence / "frozen-tasks-private.json", [0, "engine_seed"], 100
    elif mutation == "receipt":
        path, keys, value = evidence / "data/completed" / f"{match}.json", ["rows"], 1
    elif mutation in ("row_count", "owner_count", "lifecycle", "teacher_sha"):
        keys, value = {
            "row_count": (["rows"], 1), "owner_count": (["rows_by_owner", "0"], 0),
            "lifecycle": (["lifecycle", "rejected"], 1), "teacher_sha": (["source_network_sha"], "f" * 64)
        }[mutation]
        path = episode_report
    elif mutation == "terminal_audit":
        path, keys, value = evidence / "data/audit" / match / "1/native-terminal.json", ["finalized"], False
    elif mutation == "quarantine":
        atomic_json(evidence / "data/attempts" / match / "1/quarantine.json", dict(published=False))
        path = None
    elif mutation in ("cleanup", "port"):
        path, keys, value = evidence / "engine-cleanup.json", [0, "phase" if mutation == "cleanup" else "port_released"], "running" if mutation == "cleanup" else False
    elif mutation in ("dataset_identity", "split_check"):
        path = evidence / "native-migration-report.json"
        keys, value = (["dataset_identity", "dataset_sha"], "f" * 64) if mutation == "dataset_identity" else (["splits", "train", "checked_rows"], 0)
    elif mutation == "completed_exit":
        path, keys, value = target / "completion.json", ["exit_code"], 1
    else:
        path = evidence / "native-migration-update-report.json"
        keys, value = {
            "resume": (["exact_resume"], False), "freeze": (["teacher_unchanged"], False),
            "update_steps": (["expected", "steps"], 3), "checkpoint_sha": (["checkpoint_sha256"], "f" * 64),
            "train_ids": (["first_ids"], ["f" * 64]), "weights": (["weights", "greedy_nll"], 2.),
            "formal_admission": (["training_admissible"], True)
        }[mutation]
    if path is not None:
        document = json.loads(path.read_text())
        cursor = document
        for key in keys[:-1]:
            cursor = cursor[key]
        cursor[keys[-1]] = value
        atomic_json(path, document)
    with pytest.raises(ValueError):
        validate_migration_evidence(attempt, root=tmp_path)
