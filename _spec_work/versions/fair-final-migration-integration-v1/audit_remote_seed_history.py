import argparse
import hashlib
import json
from pathlib import Path

from audit_local_seed_history import REVIEWED_SOURCES, audit_history, project_jobs
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes
from native_runner.training.fair_search.contracts import require_sha


REMOTE_PREFIX = "/home/zhichun/fair-continuous-20261014/"
LOCAL_PREFIX = "fair-final-v1-20261019/historical-seed-remote-rounds-20261022/"
MANIFEST_SCHEMA = "fair-clean-student-loop.v1"
KNOWN_GAPS = (
    "Only reviewed local manifests and remote continuous rounds 1/2/3 are covered.",
    "Nested evaluation schedules, teacher sampling evaluations and legacy experiments remain unverified.",
    "Planned jobs are excluded conservatively; task completion is not established.",
    "Current remote readback does not establish historical preregistration or execution-code identity.",
    "No formal collection registration or G2 admission is created.",
)


def pinned_bytes(path, expected_sha):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != require_sha(expected_sha):
        raise ValueError("pinned historical source identity differs")
    return raw


def review_remote_rounds(evidence_root, receipt_path, bundle_path, *, receipt_sha, bundle_sha):
    evidence_root = Path(evidence_root)
    receipt_raw = pinned_bytes(receipt_path, receipt_sha)
    bundle_raw = pinned_bytes(bundle_path, bundle_sha)
    receipt, bundle = json.loads(receipt_raw), json.loads(bundle_raw)
    if not isinstance(receipt, dict) or not isinstance(bundle, dict):
        raise ValueError("remote source receipt and bundle objects required")
    if (receipt.get("schema") != "FairFinalReadOnlySeedSourceReceipt/v1"
            or receipt.get("host") != "172.16.48.35"
            or receipt.get("bundle_sha256") != bundle_sha
            or any(receipt.get(field) is not False for field in (
                "historical_preregistration_proven", "training_admissible", "formal_g2_pass"))):
        raise ValueError("remote source receipt scope or bundle identity differs")
    records = receipt.get("files")
    if not isinstance(records, list) or len(records) != 3:
        raise ValueError("all three reviewed remote rounds required")
    by_remote = {}
    for record in records:
        if not isinstance(record, dict) or record.get("remote") in by_remote:
            raise ValueError("duplicate or invalid remote source record")
        by_remote[record["remote"]] = record
    reviewed = []
    for number in (1, 2, 3):
        filename = f"round-{number:06d}-manifest.json"
        remote = REMOTE_PREFIX + f"round-{number:06d}/manifest.json"
        record = by_remote.get(remote, {})
        if record.get("local") != filename or record.get("bundle_document_equal") is not True:
            raise ValueError("remote source path or comparison evidence differs")
        raw = pinned_bytes(evidence_root / LOCAL_PREFIX / filename, record["sha256"])
        if type(record.get("bytes")) is not int or len(raw) != record["bytes"]:
            raise ValueError("remote source byte count differs")
        document = json.loads(raw)
        seeds, _ = project_jobs(document, expected_schema=MANIFEST_SCHEMA, seed_key="match_seed")
        if json_bytes(document) != json_bytes(bundle.get(remote)):
            raise ValueError("remote source differs from historical bundle document")
        if number == 1:
            local = json.loads((evidence_root / "continuous-training-20261014/round-000001-manifest.json").read_bytes())
            if json_bytes(local) != json_bytes(document):
                raise ValueError("round one remote and reviewed local manifests differ")
        reviewed.append(dict(remote=remote, file_sha256=record["sha256"], bytes=len(raw),
            jobs=len(seeds), unique_seeds=len(set(seeds)),
            existing_local_equivalent=number == 1))
    return dict(receipt_sha256=receipt_sha, bundle_sha256=bundle_sha, reviewed=reviewed)


def audit_remote_history(evidence_root, output, *, receipt_path, bundle_path, receipt_sha, bundle_sha):
    review = review_remote_rounds(evidence_root, receipt_path, bundle_path,
        receipt_sha=receipt_sha, bundle_sha=bundle_sha)
    sources = REVIEWED_SOURCES + tuple(("training", LOCAL_PREFIX + f"round-{number:06d}-manifest.json",
        MANIFEST_SCHEMA, "match_seed") for number in (2, 3))
    output = Path(output)
    audit = audit_history(evidence_root, output, sources=sources, known_gaps=KNOWN_GAPS)
    repeated = review_remote_rounds(evidence_root, receipt_path, bundle_path,
        receipt_sha=receipt_sha, bundle_sha=bundle_sha)
    if repeated != review:
        raise ValueError("remote source review changed during inventory creation")
    report = dict(schema="FairFinalRemoteHistoricalSeedAudit/v1", status="verified_partial_inventory",
        source_review=review, inventory_audit_sha256=hashlib.sha256((output / "audit.json").read_bytes()).hexdigest(),
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        unique_seeds_by_scope=audit["unique_seeds_by_scope"],
        unique_excluded_seeds=audit["unique_excluded_seeds"],
        historical_census_complete=False, historical_preregistration_proven=False,
        collection_registration_created=False, training_admissible=False, formal_g2_pass=False)
    ensure_immutable(output / "remote-review-complete.json", json_bytes(report))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha", required=True)
    args = parser.parse_args()
    report = audit_remote_history(args.evidence_root, args.output, receipt_path=args.receipt,
        bundle_path=args.bundle, receipt_sha=args.receipt_sha, bundle_sha=args.bundle_sha)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
