"""Compare complete private native snapshots; publish only aggregate evidence."""

import argparse
import hashlib
import json
from pathlib import Path


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(left, right, left_receipt, right_receipt):
    for receipt in (left_receipt, right_receipt):
        if (receipt.get("ok") is not True or receipt.get("error_code") != 0
                or receipt.get("finalized") is not True or receipt.get("queue_count") != 0
                or receipt.get("start_tick") != 0 or receipt.get("stagnant") != 0):
            raise ValueError("trace requires a complete native terminal and an empty queue")
        if type(receipt.get("tick")) is not int or receipt["tick"] < 1:
            raise ValueError("invalid terminal tick")
        for key in ("snapshots", "injected", "world_result_raw"):
            if type(receipt.get(key)) is not int:
                raise ValueError("missing or invalid native receipt field: " + key)
        if receipt["injected"] < 0:
            raise ValueError("invalid injected action count")
    for key in ("tick", "snapshots", "injected", "world_result_raw"):
        if left_receipt.get(key) != right_receipt.get(key):
            raise ValueError("trace receipt differs: " + key)
    terminal_tick = left_receipt["tick"]
    expected = [f"tick-{tick:06d}.bin" for tick in range(terminal_tick + 1)]
    if left_receipt["snapshots"] != len(expected):
        raise ValueError("snapshot count does not cover every tick")
    for directory in (left, right):
        if sorted(path.name for path in directory.iterdir()) != expected:
            raise ValueError("snapshot files do not cover exactly the expected ticks")
    rows = []
    total_bytes = 0
    for tick, name in enumerate(expected):
        first, second = (left / name).read_bytes(), (right / name).read_bytes()
        if not first or first != second:
            raise ValueError("complete snapshot differs at tick " + str(tick))
        total_bytes += len(first)
        rows.append([tick, len(first), hashlib.sha256(first).hexdigest()])
    return {"schema": "e0-snapshot-comparison.v1", "status": "passed",
            "comparison": "complete stock serialized bytes; no field normalization",
            "snapshot_count": len(rows), "terminal_tick": terminal_tick,
            "injected_actions": left_receipt["injected"], "total_bytes": total_bytes,
            "trajectory_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
            "training_admissible": False, "vm_framework_free_verified": False}


def summarize_counts(evidence):
    if evidence.get("schema") != "e0-dependency-counts.v1" or not evidence.get("engine_loaded"):
        raise ValueError("missing live dependency evidence")
    labels = {}
    for entry in evidence["coverage"]:
        row = labels.setdefault(entry["label"], {"attached": False, "failed": False,
                                "count": None, "by_phase": {}, "first_phase": None})
        row[entry["status"]] = True
    earliest = {}
    for entry in evidence["counts"]:
        if type(entry["count"]) is not int or entry["count"] <= 0:
            raise ValueError("observed counts must be positive integers")
        for label in entry["labels"]:
            row = labels.setdefault(label, {"attached": True, "failed": False,
                                    "count": None, "by_phase": {}, "first_phase": None})
            row["count"] = (row["count"] or 0) + entry["count"]
            phase = entry["phase"]
            row["by_phase"][phase] = row["by_phase"].get(phase, 0) + entry["count"]
            if entry["first_sequence"] < earliest.get(label, float("inf")):
                earliest[label] = entry["first_sequence"]
                row["first_phase"] = phase
    for row in labels.values():
        if row["attached"] and not row["failed"] and row["count"] is None:
            row["count"] = 0
        if row["failed"]:
            row["count"] = None
        row["state_effect"] = "unknown; observation is not an intervention"
    return {"schema": "e0-dependency-summary.v1", "labels": labels,
            "observer_error_count": len(evidence["errors"]),
            "label_counts_additive": False,
            "label_semantics": "shared target aliases; do not sum across labels",
            "coverage_scope": evidence["scope"], "exhaustive": False,
            "vm_framework_free_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--left-receipt", type=Path, required=True)
    parser.add_argument("--right-receipt", type=Path, required=True)
    parser.add_argument("--counts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.left, args.right, json.loads(args.left_receipt.read_text()),
                    json.loads(args.right_receipt.read_text()))
    if args.counts:
        result["dependency_summary"] = summarize_counts(json.loads(args.counts.read_text()))
        result["counts_sha256"] = file_sha(args.counts)
    with args.output.open("x") as target:
        target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "dependency_summary"}))


if __name__ == "__main__":
    main()
