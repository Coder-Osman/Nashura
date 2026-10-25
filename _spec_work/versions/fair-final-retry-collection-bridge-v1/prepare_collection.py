import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
for directory in (ROOT.parent / "fair-final-migration-integration-v1",
        ROOT.parent / "fair-final-selection-timeout-v1"):
    sys.path.insert(0, str(directory))

import verify_retry
from build_collection_payload import build_from_parent
from retry_selection import checked_files, safe_tree_hash
from run_selection import GATES, checked_handoff, payload_files, pinned, publish, read, sha


SCHEMA = "FairFinalRetryCollectionHandoff/v1"
OUTPUT_FILES = {"payload.tar.gz", "collection-payload.json", "selected.pt",
    "selected.report.json", "terminal-verification.json"}


def independent_output(parent, output):
    parent, output = Path(parent), Path(output)
    if any(path.is_symlink() for endpoint in (parent, output)
            for path in (endpoint, *endpoint.parents)):
        raise ValueError("collection handoff paths must not contain symlinks")
    parent, output = parent.resolve(), output.resolve()
    if output.is_relative_to(parent) or parent.is_relative_to(output):
        raise ValueError("new collection handoff must be independent of retry parent")
    if output.exists():
        raise ValueError("new collection handoff directory required")
    return parent, output


def build(parent, *, launch_sha, receipt_sha, data_roots):
    parent = Path(parent)
    if any(path.is_symlink() for path in (parent, *parent.parents)):
        raise ValueError("collection parent paths must not contain symlinks")
    parent = parent.resolve()
    pinned(parent / "launch.json", launch_sha)
    pinned(parent / "receipt.json", receipt_sha)
    launch, receipt = read(parent / "launch.json"), read(parent / "receipt.json")
    inventory = receipt["inventory"]
    if (receipt["launch_sha256"] != launch_sha or inventory["running"]
            or inventory["lease_available"] is not True
            or receipt["status"] != "terminal_archive_ready_for_independent_verification"
            or inventory["state"] != receipt["status"]
            or any(record[name] is not value for record in (launch, receipt, inventory)
                for name, value in GATES.items())):
        raise ValueError("stopped terminal retry archive required")
    proof = verify_retry.verify(parent, launch_sha=launch_sha, receipt_sha=receipt_sha,
        data_roots=data_roots)
    if (proof["status"] != "passed_independent_retry_terminal_content_scope"
            or proof["launch_sha256"] != launch_sha or proof["receipt_sha256"] != receipt_sha
            or proof["verifier_sha256"] != sha(verify_retry.__file__)
            or proof["content"]["native_schedule_verified"] is not True
            or proof["content"]["timeout_history_prefix_verified"] is not True
            or proof["content"]["latest_optimizer_steps"] <= proof["content"]["timeout_optimizer_steps"]
            or proof["native_game_provenance_reverified"] is not False
            or any(proof[name] is not value for name, value in GATES.items())):
        raise ValueError("independently verified retry terminal content required")
    archive = parent / "archive"
    checked_files(archive, inventory["files"])
    if safe_tree_hash(archive) != inventory["files"]:
        raise ValueError("retry archive changed during collection handoff")
    selection = archive / "selection"
    completion = read(selection / "completion.json")
    report = read(selection / "selected.report.json")
    selected = (selection / "selected.pt").read_bytes()
    selected_sha = hashlib.sha256(selected).hexdigest()
    if (report["terminal"] not in ("EARLY_STOP", "MAX_EPOCHS")
            or report["checkpoint_sha"] != selected_sha
            or completion["artifacts"]["selected.pt"]["sha256"] != selected_sha):
        raise ValueError("retry terminal checkpoint identity differs")
    inputs = selection / "inputs"
    original_sha = inventory["files"]["selection/inputs/selection-handoff.json"]["sha256"]
    request = checked_handoff(inputs, original_sha)
    files, identity = payload_files(inputs, request)
    package, collection = build_from_parent(
        dict(payload_sha256=request["files"]["payload.tar.gz"]["sha256"], deterministic=True),
        files, identity, expected_launch_sha=request["parent_launch_sha256"])
    collection.update(schema="FairFinalRetryCollectionPayload/v1",
        terminal_retry_parent=dict(launch_sha256=launch_sha, receipt_sha256=receipt_sha,
            checkpoint_sha256=selected_sha, report_sha256=sha(selection / "selected.report.json"),
            timeout_checkpoint_sha256=sha(selection / "resume-input.pt"),
            verification_sha256=hashlib.sha256(encoded(proof)).hexdigest()),
        selection_content_independently_verified=True)
    result = {"payload.tar.gz": package, "collection-payload.json": encoded(collection),
        "selected.pt": selected, "selected.report.json": (selection / "selected.report.json").read_bytes(),
        "terminal-verification.json": encoded(proof)}
    checked_files(archive, inventory["files"])
    if safe_tree_hash(archive) != inventory["files"]:
        raise ValueError("retry archive changed while preparing collection payload")
    pinned(parent / "launch.json", launch_sha)
    pinned(parent / "receipt.json", receipt_sha)
    return result


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def write_new(path, data):
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def prepare(parent, *, output, launch_sha, receipt_sha, data_roots):
    parent, output = independent_output(parent, output)
    files = build(parent, launch_sha=launch_sha, receipt_sha=receipt_sha, data_roots=data_roots)
    output.mkdir()
    try:
        descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        for name, data in files.items():
            write_new(output / name, data)
        inventory = safe_tree_hash(output)
        expected = {name: dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            for name, data in files.items()}
        if set(inventory) != OUTPUT_FILES or inventory != expected:
            raise ValueError("collection handoff file inventory differs")
        report = dict(schema=SCHEMA, status="prepared_only_not_executed",
            parent_launch_sha256=launch_sha, parent_receipt_sha256=receipt_sha,
            builder_sha256=sha(__file__), files=inventory,
            requires_registered_tasks=True, requires_parent_data_preflight=True,
            native_collection_executed=False, **GATES)
        publish(output / "handoff.json", report)
        return report
    except BaseException as error:
        publish(output / "failure.json", dict(status="failed_quarantined", error=repr(error), **GATES))
        raise


def verify(output, *, expected_sha, parent, launch_sha, receipt_sha, data_roots):
    output = Path(output)
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("collection handoff paths must not contain symlinks")
    output = output.resolve()
    pinned(output / "handoff.json", expected_sha)
    report = read(output / "handoff.json")
    if (report["schema"] != SCHEMA or report["status"] != "prepared_only_not_executed"
            or report["parent_launch_sha256"] != launch_sha
            or report["parent_receipt_sha256"] != receipt_sha
            or report["builder_sha256"] != sha(__file__)
            or report["requires_registered_tasks"] is not True
            or report["requires_parent_data_preflight"] is not True
            or report["native_collection_executed"] is not False
            or any(report[name] is not value for name, value in GATES.items())
            or set(report["files"]) != OUTPUT_FILES):
        raise ValueError("prepared-only collection handoff scope differs")
    checked_files(output, report["files"])
    if set(safe_tree_hash(output)) != OUTPUT_FILES | {"handoff.json"}:
        raise ValueError("collection handoff has unreceipted files")
    expected = build(parent, launch_sha=launch_sha, receipt_sha=receipt_sha, data_roots=data_roots)
    if any((output / name).read_bytes() != data for name, data in expected.items()):
        raise ValueError("collection handoff differs from independently rebuilt parent")
    checked_files(output, report["files"])
    pinned(output / "handoff.json", expected_sha)
    if set(safe_tree_hash(output)) != OUTPUT_FILES | {"handoff.json"}:
        raise ValueError("collection handoff changed during verification")
    return dict(status="passed_retry_collection_handoff_only", handoff_sha256=expected_sha,
        parent_launch_sha256=launch_sha, parent_receipt_sha256=receipt_sha,
        native_collection_executed=False, **GATES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = vars(parser.parse_args())
    report = prepare(**args)
    print(json.dumps({name: report[name] for name in ("status", *GATES)}), flush=True)


if __name__ == "__main__":
    main()
