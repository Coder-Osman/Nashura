import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

from verify_active import implementation_identity, verify_archive
from verify_execution import pinned, read
from verify_native_resume import checkpoint_state, sha
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest


CHECKPOINT = "active-comparison/trainable-reference.pt"
SCHEMA = "FairFinalActiveSelectionHandoff/v1"


def frozen_files(evidence, launch):
    package_path = evidence / "payload.tar.gz"
    pinned(package_path, launch["payload_sha256"])
    package = package_path.read_bytes()
    files = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if (not member.isfile() or relative.is_absolute() or ".." in relative.parts
                    or str(relative) != member.name or member.name in files):
                raise ValueError("selection payload member differs")
            files[member.name] = archive.extractfile(member).read()
    manifest = read(evidence / "deployment.json")
    actual = {name: digest(data) for name, data in files.items()
        if name != "deployment.json"}
    if (json.loads(files["deployment.json"]) != manifest or actual != manifest
            or manifest != launch["source_sha256"]):
        raise ValueError("selection payload differs from verified frozen deployment")
    for name, expected in manifest.items():
        pinned(evidence / name, expected)
    return package


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(parent, *, launch_sha, receipt_sha, data_roots, output):
    parent, output = Path(parent), Path(output)
    if output.exists():
        raise ValueError("selection handoff output already exists")
    if output.resolve().is_relative_to(parent.resolve()):
        raise ValueError("selection handoff must be independent of parent archive")
    verification = verify_archive(parent, launch_sha=launch_sha, receipt_sha=receipt_sha,
        data_roots=data_roots)
    if (verification["status"] != "passed_active_archive_and_two_stage_checkpoint_scope"
            or verification["content"]["native_schedule_verified"] is not True
            or verification["frozen_deployment_verified"] is not True
            or any(verification[name] is not False for name in
                ("training_admissible", "formal_g2_pass", "gates_closed"))):
        raise ValueError("selection requires independently verified native active recovery")
    pinned(parent / "launch.json", launch_sha)
    pinned(parent / "native-download-receipt.json", receipt_sha)
    launch = read(parent / "launch.json")
    evidence = parent / "native-evidence"
    receipt = read(parent / "native-download-receipt.json")
    package = frozen_files(evidence, launch)
    checkpoint_sha = receipt["files"][CHECKPOINT]["sha256"]
    checkpoint_path = evidence / CHECKPOINT
    state, identity, _ = checkpoint_state(checkpoint_path, checkpoint_sha)
    expected = verification["content"]["branches"]["trainable"]["state"]
    if (state != expected or state["steps"] != 7 or identity != verification["dataset_identity"]):
        raise ValueError("selection parent checkpoint differs from verified step seven")
    implementation = implementation_identity(evidence)
    files = {"payload.tar.gz": package, "parent-checkpoint.pt": checkpoint_path.read_bytes(),
        "parent-verification.json": json_bytes(verification)}
    if digest(files["parent-checkpoint.pt"]) != checkpoint_sha:
        raise ValueError("selection parent checkpoint changed during handoff")
    request = dict(schema=SCHEMA, parent_launch_sha256=launch_sha,
        parent_receipt_sha256=receipt_sha, parent_checkpoint_sha256=checkpoint_sha,
        parent_checkpoint_state=state, parent_remote=launch["remote"],
        implementation_sha=implementation, dataset_identity=identity,
        split_rows=verification["split_rows"], series_sha=verification["series_sha"],
        config=read(evidence / "selection-config.json"),
        loss_weights=read(evidence / "loss-weights.json"),
        files={name: dict(sha256=digest(data), bytes=len(data))
            for name, data in files.items()},
        verifier_sha256=sha(Path(__file__).with_name("verify_active.py")),
        preparer_sha256=sha(__file__),
        status="prepared_only_not_executed", resume_optimizer_rng_cursor_history=True,
        max_updates=None, training_admissible=False, formal_g2_pass=False, gates_closed=False)
    request["handoff_sha"] = semantic_digest(request)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        durable_write(output / name, data)
    durable_write(output / "selection-handoff.json", json_bytes(request))
    return request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.parent, launch_sha=args.launch_sha, receipt_sha=args.receipt_sha,
        data_roots=args.data_roots, output=args.output)
    print(json.dumps(dict(status=report["status"], handoff_sha=report["handoff_sha"],
        output=str(args.output), training_admissible=False)), flush=True)


if __name__ == "__main__":
    main()
