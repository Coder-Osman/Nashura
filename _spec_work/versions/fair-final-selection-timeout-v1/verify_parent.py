import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-active-migration-v1"))

import torch

from run_selection import GATES, SCHEMA, checked_handoff, invocation, payload_files, pinned, read, sha
from selection_ssh import (expected_remote_tree, safe_relative, validate_launch,
    validate_remote_metadata, verify_remote_mirror)
from verify_active import checked_checkpoint, implementation_identity
from verify_selection_execution import checked_cursor
from verify_pipeline import verified_dataset
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest


def checkpoint_content(path, *, expected_sha, original_path, request, dataset, teacher_sha):
    options = dict(dataset=dataset, implementation_sha=request["implementation_sha"],
        config=request["config"], weights=request["loss_weights"], teacher_sha=teacher_sha)
    original_state, _, original_cursor = checked_checkpoint(original_path,
        request["parent_checkpoint_sha256"], **options)
    state, _, cursor = checked_checkpoint(path, expected_sha, **options)
    original = torch.load(original_path, weights_only=True, map_location="cpu")["payload"]
    payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
    fixed = ("seed", "batch_size", "micro_batch_size", "frozen_epochs", "trainable_epochs")
    prefix = original["history"]
    if (original_state != request["parent_checkpoint_state"] or original_state["steps"] != 7
            or state["steps"] <= original_state["steps"]
            or any(cursor[name] != original_cursor[name] for name in fixed)
            or payload["history"][:len(prefix)] != prefix):
        raise ValueError("timeout checkpoint does not continue its verified parent")
    best = payload["best"]["payload"]
    best_cursor = checked_cursor(best, dataset, cursor)
    selected = payload["history"][payload["best_index"]]
    if (best_cursor["completed_epochs"] != selected["epoch"]
            or best_cursor["optimizer_steps"] != selected["optimizer_steps"]
            or best["teacher_network_sha"] != teacher_sha
            or best["weights"] != request["loss_weights"]
            or best.get("lineage") != payload["latest"]["payload"].get("lineage")):
        raise ValueError("timeout selected trainer differs from validation history")
    if (payload["best_index"] < len(prefix)
            and semantic_digest(payload["best"]) != semantic_digest(original["best"])):
        raise ValueError("timeout checkpoint changed an inherited best trainer")
    return dict(state=state, completed_epochs=cursor["completed_epochs"], offset=cursor["offset"],
        consumed=cursor["consumed"], selected_epoch=selected["epoch"],
        history_sha256=semantic_digest(payload["history"]), best_sha256=semantic_digest(payload["best"]))


def verify(parent, *, launch_sha, receipt_sha, data_roots):
    parent, launch = validate_launch(parent, launch_sha)
    pinned(parent / "native-download-receipt.json", receipt_sha)
    receipt = read(parent / "native-download-receipt.json")
    inventory = receipt["remote_inventory"]
    if (receipt["schema"] != "FairFinalActiveSelectionRemoteReceipt/v1"
            or receipt["launch_sha256"] != launch_sha
            or receipt["status"] != "failed_quarantined"
            or inventory["state"] != "failed_quarantined"
            or inventory.get("running") or receipt["completion_sha256"] is not None
            or any(record[name] is not value for record in (receipt, inventory)
                for name, value in GATES.items()) or len(data_roots) != 1):
        raise ValueError("preserved stopped selection-timeout archive required")
    validate_remote_metadata(parent, launch, inventory)
    if receipt["metadata"] != inventory["metadata"]:
        raise ValueError("timeout metadata receipt differs")
    expected = expected_remote_tree(parent, launch)
    verify_remote_mirror(inventory["files"], expected)
    downloaded = {name: metadata for name, metadata in inventory["files"].items()
        if Path(name).parts[0] not in ("inputs", "deployment")}
    if receipt["downloaded_files"] != downloaded:
        raise ValueError("timeout download inventory differs")
    directory = parent / "selection"
    for name, metadata in inventory["files"].items():
        safe_relative(name)
        path = directory / name
        pinned(path, metadata["sha256"])
        if path.stat().st_size != metadata["bytes"]:
            raise ValueError("timeout archive byte count differs")
    if any((directory / name).exists() for name in ("completion.json", "selected.report.json")):
        raise ValueError("terminal selection is not a timeout retry source")
    execution, failure = read(directory / "execution.json"), read(directory / "failure.json")
    request = checked_handoff(directory / "inputs", execution["handoff_file_sha256"])
    _, manifest = payload_files(directory / "inputs", request)
    command, environment = invocation(execution["root"], source=launch["source"],
        data_root=launch["data_root"], device="cuda:0", python=launch["remote_python"])
    command += ["--resume-sha", request["parent_checkpoint_sha256"]]
    if (execution["schema"] != SCHEMA or execution["root"] != launch["remote_root"] + "/selection"
            or execution["source"] != launch["source"] or execution["data_root"] != launch["data_root"]
            or execution["source_sha256"] != manifest or manifest != launch["deployment_manifest"]
            or execution["command"] != command or execution["environment"] != environment
            or execution["device"] != "cuda:0" or execution["python"] != launch["remote_python"]
            or execution["worker_sha256"] != launch["worker_sha256"]
            or execution["handoff_file_sha256"] != launch["handoff_sha256"]
            or execution["handoff_sha"] != request["handoff_sha"]
            or execution["parent_checkpoint_sha256"] != request["parent_checkpoint_sha256"]
            or receipt["execution_sha256"] != sha(directory / "execution.json")
            or execution["timeout_seconds"] != launch["timeout_seconds"]
            or not 1800 <= execution["timeout_seconds"] <= 28800
            or failure["status"] != "failed_quarantined"
            or failure["error"] != "RuntimeError('selection child exit code: 124')"
            or type(failure["seconds"]) not in (int, float)
            or not execution["timeout_seconds"] <= failure["seconds"] <= execution["timeout_seconds"] + 60
            or any(record[name] is not value for record in (execution, failure)
                for name, value in GATES.items())):
        raise ValueError("timeout frozen execution or failure differs")
    dataset, audit, source = verified_dataset(read(directory / "deployment/private-plan.json"), data_roots)
    if (implementation_identity(directory / "deployment") != request["implementation_sha"]
            or dataset.identity != request["dataset_identity"] or audit["series_sha"] != request["series_sha"]
            or {name: len(rows) for name, rows in dataset.splits.items()} != request["split_rows"]):
        raise ValueError("timeout rebuilt dataset or implementation differs")
    content = checkpoint_content(directory / "selected.pt", expected_sha=sha(directory / "selected.pt"),
        original_path=directory / "inputs/parent-checkpoint.pt", request=request,
        dataset=dataset, teacher_sha=source["source_network_sha"])
    if (content["state"]["steps"] > 24 or dataset.splits["train"] == []):
        raise ValueError("timeout update budget differs")
    return dict(schema="FairFinalSelectionTimeoutVerification/v1",
        status="passed_timeout_archive_and_checkpoint_content_only", parent_launch_sha256=launch_sha,
        parent_receipt_sha256=receipt_sha, failure_sha256=sha(directory / "failure.json"),
        execution_sha256=sha(directory / "execution.json"), checkpoint_sha256=sha(directory / "selected.pt"),
        content=content, dataset_identity=dataset.identity, implementation_sha=request["implementation_sha"],
        native_game_provenance_reverified=False, live_resume_verified=False, verifier_sha256=sha(__file__), **GATES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.parent, launch_sha=args.launch_sha, receipt_sha=args.receipt_sha,
        data_roots=args.data_roots)
    durable_write(args.output, json_bytes(report))
    print(json_bytes(report).decode(), flush=True)
