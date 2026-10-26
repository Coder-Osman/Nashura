"""Fail closed on incomplete E1 evidence; never infer VM independence."""

import argparse
import json
from pathlib import Path

from verify_e0_snapshots import summarize_counts

RESOURCE_IMPORTS = {
    "AAssetManager_fromJava", "AAssetManager_open", "AAsset_read", "AAsset_close",
    "AAsset_seek64", "AAsset_getLength", "AAsset_getLength64",
    "AAsset_getRemainingLength64", "AAsset_openFileDescriptor64",
}
SEMANTIC_CHECKS = ("bytes", "read", "seek", "length", "descriptor", "missing",
                   "namespace_priority", "fallback", "snapshot_restore", "reset",
                   "identities_unchanged")


def evaluate(comparison, counts, redirect, semantics):
    summary = summarize_counts(counts)
    reasons = []
    if (comparison.get("schema") != "e0-snapshot-comparison.v1"
            or comparison.get("status") != "passed"
            or comparison.get("snapshot_count", 0) < 2):
        reasons.append("complete native trajectory comparison missing")
    receipt, metrics = redirect.get("receipt", {}), redirect.get("metrics") or {}
    if receipt.get("installed") is not True or receipt.get("errors") != []:
        reasons.append("resource redirect installation incomplete")
    entries = receipt.get("redirects", [])
    if ({entry.get("name") for entry in entries} != RESOURCE_IMPORTS
            or len(entries) != len(RESOURCE_IMPORTS)
            or any(entry.get("verified") is not True for entry in entries)):
        reasons.append("resource import verification incomplete")
    if metrics.get("failures") != "0" or metrics.get("live_assets") != "0":
        reasons.append("provider failure or live asset remains")
    resource_labels = {name: row for name, row in summary["labels"].items()
                       if name.startswith("AAsset")}
    if not RESOURCE_IMPORTS.issubset(resource_labels):
        reasons.append("required platform resource hook coverage missing")
    if any(not row["attached"] or row["failed"] or row["count"] is None
           for row in resource_labels.values()):
        reasons.append("platform resource hook failed or uncovered")
    actual_calls = sum(row["count"] for row in counts["counts"]
                       if any(name.startswith("AAsset") for name in row["labels"]))
    if actual_calls:
        reasons.append("actual platform AssetManager events remain")
    missing = [name for name in SEMANTIC_CHECKS if semantics.get(name) is not True]
    if missing:
        reasons.append("unverified semantics: " + ", ".join(missing))
    return {"schema": "e1-acceptance.v1", "status": "blocked" if reasons else "passed",
            "reasons": reasons, "actual_assetmanager_events": actual_calls,
            "unverified_semantics": missing,
            "coverage_scope": "observed exports and configured hooks; not exhaustive",
            "vm_framework_free_verified": False, "training_admissible": False,
            "tenfold_speedup_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("comparison", "counts", "redirect", "semantics", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(*(json.loads(getattr(args, name).read_text())
                        for name in ("comparison", "counts", "redirect", "semantics")))
    with args.output.open("x") as target:
        target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 2)


if __name__ == "__main__":
    main()
