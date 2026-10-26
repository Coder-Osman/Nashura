"""Fail closed on incomplete E1 evidence; never infer VM independence."""

import argparse
import hashlib
import json
from pathlib import Path

from verify_e0_snapshots import summarize_counts
from verify_e1_semantics import SEMANTIC_CHECKS, load_attachments, verify_semantics

RESOURCE_IMPORTS = {
    "AAssetManager_fromJava", "AAssetManager_open", "AAsset_read", "AAsset_close",
    "AAsset_seek64", "AAsset_getLength", "AAsset_getLength64",
    "AAsset_getRemainingLength64", "AAsset_openFileDescriptor64",
}
EXPECTED_TRAJECTORY = dict(snapshot_count=3602, terminal_tick=3601, injected_actions=28,
    total_bytes=9120440,
    trajectory_sha256="0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5")
DESTROY_LABELS = {"GameStateDestroy", "GameStateBaseDestroy", "LogicCommandQueueDestroy"}


def evaluate(comparison, counts, redirect, semantics, attachments=None, counts_sha256=None):
    summary = summarize_counts(counts)
    reasons = []
    if (comparison.get("schema") != "e0-snapshot-comparison.v1"
            or comparison.get("status") != "passed"
            or any(comparison.get(name) != value for name, value in EXPECTED_TRAJECTORY.items())):
        reasons.append("complete native trajectory comparison missing")
    if not counts_sha256 or comparison.get("counts_sha256") != counts_sha256:
        reasons.append("trajectory is not bound to supplied counts bytes")
    receipt, metrics = redirect.get("receipt", {}), redirect.get("metrics") or {}
    if receipt.get("installed") is not True or receipt.get("errors") != []:
        reasons.append("resource redirect installation incomplete")
    entries = receipt.get("redirects", [])
    if ({entry.get("name") for entry in entries} != RESOURCE_IMPORTS
            or len(entries) != len(RESOURCE_IMPORTS)
            or any(entry.get("verified") is not True for entry in entries)):
        reasons.append("resource import verification incomplete")
    if (metrics.get("failures") != "0" or metrics.get("live_assets") != "0"
            or metrics.get("entries") != "9829" or int(metrics.get("calls", "0")) <= 0):
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
    destroyed = {name for row in counts["counts"]
                 if row.get("host_phase") == "terminal_reset_destroy" and row["count"] > 0
                 for name in row["labels"] if name in row.get("native_scopes", [])}
    if not DESTROY_LABELS <= destroyed:
        reasons.append("terminal battle destruction coverage missing")
    checks, semantic_errors = verify_semantics(semantics, attachments or {})
    missing = [name for name in SEMANTIC_CHECKS if checks[name] is not True]
    if missing:
        reasons.append("unverified semantics: " + ", ".join(missing))
    return {"schema": "e1-acceptance.v2", "status": "blocked" if reasons else "passed",
            "reasons": reasons, "actual_assetmanager_events": actual_calls,
            "unverified_semantics": missing,
            "semantic_errors": semantic_errors, "semantic_checks": checks,
            "coverage_scope": "cold startup through terminal, battle destruction and final reset; configured hooks only",
            "uncovered": ["process teardown", "natural FMOD audio playback", "unconfigured indirect targets"],
            "vm_framework_free_verified": False, "training_admissible": False,
            "tenfold_speedup_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("comparison", "counts", "redirect", "semantics", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    hashes = {}
    try:
        inputs = [json.loads(getattr(args, name).read_text())
                  for name in ("comparison", "counts", "redirect", "semantics")]
        attachments, hashes = load_attachments(inputs[3], args.semantics.parent)
        result = evaluate(*inputs, attachments=attachments,
                          counts_sha256=hashlib.sha256(args.counts.read_bytes()).hexdigest())
    except (OSError, KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        result = dict(schema="e1-acceptance.v2", status="blocked",
            reasons=["invalid acceptance evidence: " + str(error)],
            vm_framework_free_verified=False, training_admissible=False, tenfold_speedup_verified=False)
    result["attachment_sha256"] = hashes
    with args.output.open("x") as target:
        target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 2)


if __name__ == "__main__":
    main()
