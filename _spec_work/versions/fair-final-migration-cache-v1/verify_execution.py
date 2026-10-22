import argparse
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "fair-final-migration-integration-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

import torch

from migration_series import audit_completed_series, ReadOnlyMigrationStore
from verify_native_resume import checkpoint_state, verify_comparison, sha
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


SERIES = "_spec_work/versions/fair-final-migration-series-v1/"
SELECTION = "_spec_work/versions/fair-final-migration-selection-v1/"
PROBE = "_spec_work/versions/fair-final-migration-integration-v1/probe_native_resume.py"


def read(path):
    return json.loads(Path(path).read_text())


def pinned(path, expected):
    if sha(path) != require_sha(expected):
        raise ValueError("pinned artifact differs: " + str(path))


def archived(attempt, *, launch_sha, receipt_sha):
    attempt = Path(attempt)
    pinned(attempt / "launch.json", launch_sha)
    pinned(attempt / "native-download-receipt.json", receipt_sha)
    launch = read(attempt / "launch.json")
    receipt = read(attempt / "native-download-receipt.json")
    if (receipt["launch_sha256"] != launch_sha or receipt["remote"] != launch["remote"]
            or receipt["training_admissible"] is not False or receipt["formal_g2_pass"] is not False):
        raise ValueError("archive launch or engineering scope differs")
    evidence = attempt / "native-evidence"
    for name, metadata in receipt["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("archive path escapes evidence")
        path = evidence / name
        if path.stat().st_size != metadata["bytes"]:
            raise ValueError("archived artifact size differs")
        pinned(path, metadata["sha256"])
    required = dict(launch.get("source_sha256", {}))
    required.update(launch.get("uploaded_sha256", {}))
    if "supervisor_sha256" in launch:
        required["supervisor.py"] = launch["supervisor_sha256"]
    for name, expected in required.items():
        if receipt["files"].get(name, {}).get("sha256") != expected:
            raise ValueError("archive omitted or changed frozen deployment")
    deployment = read(evidence / "deployment.json")
    if "source_sha256" in launch:
        if deployment != launch["source_sha256"]:
            raise ValueError("parent deployment manifest differs")
    else:
        package = (evidence / "payload.tar.gz").read_bytes()
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            original = json.loads(archive.extractfile("deployment.json").read())
        if original != deployment:
            raise ValueError("resume deployment differs from frozen parent package")
    for name, expected in deployment.items():
        if receipt["files"].get(name, {}).get("sha256") != expected:
            raise ValueError("archive deployment coverage differs")
    return launch, evidence, receipt


def implementation_identity(evidence):
    paths = [SERIES + "series_training.py", SERIES + "migration_series.py",
        "firstlight_CR/native_runner/training/fair_migration_trainer.py"]
    if (evidence / SERIES / "verified_store.py").exists():
        paths.append(SERIES + "verified_store.py")
    if (evidence / SERIES / "deterministic_runtime.py").exists():
        paths.append(SERIES + "deterministic_runtime.py")
    series_sha = semantic_digest({Path(name).name: sha(evidence / name) for name in paths})
    return semantic_digest(dict(selection=sha(evidence / SELECTION / "round_selection.py"),
        entrypoint=sha(evidence / SELECTION / "train_round.py"), series=series_sha))


def verify_execution(parent, attempt, *, parent_launch_sha, parent_receipt_sha,
        resume_launch_sha, resume_receipt_sha, data_roots, native_data_attempt=None):
    parent_launch, parent_evidence, _ = archived(parent, launch_sha=parent_launch_sha, receipt_sha=parent_receipt_sha)
    launch, evidence, receipt = archived(attempt, launch_sha=resume_launch_sha, receipt_sha=resume_receipt_sha)
    settings = read(evidence / "resume-settings.json")
    if (launch["parent_launch_sha256"] != parent_launch_sha or settings != launch["settings"]
            or settings["parent"] != parent_launch["remote"] or settings["parent_process"] != parent_launch["process"]
            or launch["uploaded_sha256"]["payload.tar.gz"] != parent_launch["payload_sha256"]):
        raise ValueError("resume parent linkage differs")
    command = parent_launch["command"]
    if (settings["source"] != command[command.index("--source") + 1]
            or settings["data_root"] != command[command.index("--data-roots") + 1]):
        raise ValueError("resume source or data command differs")
    for name in ("deployment.json", "private-plan.json", "selection-config.json", "loss-weights.json"):
        if sha(parent_evidence / name) != sha(evidence / name):
            raise ValueError("parent and resume contracts differ")
    completion = read(parent_evidence / "completion.json")
    if completion["exit_code"] != 0 or completion["training_admissible"] is not False:
        raise ValueError("parent did not complete successfully")
    for name in ("round0.pt", "round0.report.json", "completion.json"):
        if sha(parent_evidence / name) != sha(evidence / ("parent-" + name)):
            raise ValueError("parent snapshot differs")
    parent_state, identity, splits = checkpoint_state(parent_evidence / "round0.pt", sha(evidence / "parent-round0.pt"))
    parent_report = read(parent_evidence / "round0.report.json")
    transfer = read(evidence / "parent-receipt.json")
    if (parent_state["steps"] != 1 or parent_report["latest_optimizer_steps"] != 1
            or parent_report["checkpoint_sha"] != sha(parent_evidence / "round0.pt")
            or parent_report["terminal"] is not None or parent_report["dataset_identity"] != identity
            or transfer["checkpoint_sha256"] != parent_report["checkpoint_sha"]
            or transfer["report_sha256"] != sha(parent_evidence / "round0.report.json")
            or transfer["completion_sha256"] != sha(parent_evidence / "completion.json")
            or transfer["parent"] != parent_launch["remote"] or transfer["dataset_identity"] != identity):
        raise ValueError("parent checkpoint or receipt accounting differs")
    result = read(evidence / "completion.json")
    phases = read(evidence / "phase-results.json")
    if (result["status"] != "passed_pending_independent_verification" or result["phases"] != phases
            or [phase["phase"] for phase in phases] != ["reference", "resumed"]
            or any(phase["exit_code"] != 0 for phase in phases)
            or phases[0]["process"] == phases[1]["process"]
            or result["training_admissible"] is not False or result["formal_g2_pass"] is not False):
        raise ValueError("fresh-process execution phases differ")
    comparison = evidence / "comparison"
    reference = read(comparison / "reference.json")
    resumed = read(comparison / "resumed.json")
    if (reference["before"] != parent_state or reference["input_checkpoint_sha256"] != parent_report["checkpoint_sha"]
            or resumed["input_checkpoint_sha256"] != reference["midpoint_sha256"]
            or reference["dataset_identity"] != identity or resumed["dataset_identity"] != identity
            or reference["probe_sha256"] != sha(evidence / PROBE) or resumed["probe_sha256"] != sha(evidence / PROBE)):
        raise ValueError("comparison inputs differ from parent or frozen probe")
    implementation_sha = implementation_identity(evidence)
    for path in (parent_evidence / "round0.pt", comparison / "midpoint.pt", comparison / "reference.pt", comparison / "resumed.pt"):
        payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
        if payload["implementation_sha"] != implementation_sha:
            raise ValueError("checkpoint implementation differs from archived source")
    plan = read(evidence / "private-plan.json")
    audit = audit_completed_series(plan, data_roots)
    actual_splits = {name: [] for name in splits}
    receipts = {}
    for entry, root in zip(plan["rounds"], data_roots):
        store = ReadOnlyMigrationStore(root, manifest=entry["manifest"])
        for match, task in sorted(store.manifest["matches"].items()):
            episode, record = store.load(match)
            receipts[match] = semantic_digest(record)
            actual_splits[task["split"]].extend(row["row_id"] for row in episode["rows"])
    if len(plan["rounds"]) != 1:
        raise ValueError("initial-round engineering verification requires one round")
    source = plan["rounds"][0]["manifest"]["identity"]
    data_identity = dict(source_a_sha=source["source_a_sha"], migration_adapter_sha=source["migration_adapter_sha"],
        dataset_sha=semantic_digest(dict(manifest_sha=store.dataset_sha, receipts=receipts)))
    if (identity != data_identity or splits != actual_splits or source["source_network_sha"] != parent_state["teacher"]
            or parent_report["series_sha"] != audit["series_sha"]):
        raise ValueError("checkpoint data, split or teacher differs from archived whole games")
    content = verify_comparison(comparison, reference_sha=sha(comparison / "reference.json"),
        resumed_sha=sha(comparison / "resumed.json"))
    provenance = None
    if native_data_attempt is not None:
        evidence_root = ROOT.parents[1] / "evidence/fair-final-v1-20261019"
        sys.path.insert(0, str(evidence_root))
        from verify_migration_evidence import validate_migration_evidence
        native_data_attempt = Path(native_data_attempt).resolve()
        if (len(data_roots) != 1 or Path(data_roots[0]).resolve() != native_data_attempt / "evidence/data"
                or native_data_attempt.parent != evidence_root.resolve()):
            raise ValueError("native data provenance path differs")
        provenance = validate_migration_evidence(native_data_attempt.name)
        if provenance["dataset_identity"] != identity or provenance["status"] != "passed":
            raise ValueError("native game provenance identity differs")
    return dict(status="passed_execution_archive_and_checkpoint_scope", content=content,
        implementation_sha=implementation_sha, dataset_identity=identity,
        split_rows={name: len(rows) for name, rows in actual_splits.items()},
        parent_launch_sha256=parent_launch_sha, resume_launch_sha256=resume_launch_sha,
        parent_receipt_sha256=parent_receipt_sha, resume_receipt_sha256=resume_receipt_sha,
        frozen_deployment_verified=True, native_game_provenance_reverified=provenance is not None,
        native_provenance_report_sha=semantic_digest(provenance) if provenance else None,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("attempt", type=Path)
    for name in ("parent-launch-sha", "parent-receipt-sha", "resume-launch-sha", "resume-receipt-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--native-data-attempt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    options = vars(args).copy()
    output = options.pop("output")
    report = verify_execution(**options)
    report["verifier_sha256"] = sha(__file__)
    durable_write(output, json_bytes(report))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
