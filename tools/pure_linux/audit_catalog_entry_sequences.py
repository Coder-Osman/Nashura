"""Compare bounded real startup observations without publishing entry values."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from compare_catalog_update_assets import parse_inventory


ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
BATCHES = [[0, 0, 0, start, 39] for start in (0, 39, 78, 117, 156)]
NAMES = ("entry_type", "entry_id")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sequences(android, linux):
    trace = android["trace"]
    if (android.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            android.get("engine_sha256") != ENGINE or android.get("catalog_ready_pass") is not False or
            android.get("update_unchanged") is not True or android.get("cleanup_error") or
            android.get("resume") != "returned" or android.get("script_error_count") != 0 or
            android.get("spawn_gate", {}).get("enabled") is not True or
            android.get("spawn_gate", {}).get("disabled") is not True or
            android.get("launch", {}).get("exit") != 0 or trace.get("errors") != [] or
            trace.get("lost") != 0 or trace.get("activeScopes") != 0 or trace.get("activeBatchScopes") != 0):
        raise ValueError("Android capture incomplete")
    targets = [event for event in android["spawn_gate"].get("events", [])
               if event.get("identifier") == "nullsroyale.rel.free:engine0"]
    if len(targets) != 1 or targets[0].get("pid") != android.get("owned_pid"):
        raise ValueError("Android gated process identity mismatch")
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if not all(coverage.get(name) is True for name in ("table_batch", *NAMES)):
        raise ValueError("Android hook coverage incomplete")
    events = trace["events"]
    if not events or events[0].get("kind") != "engine_hooks_ready" or any(
            row.get("sequence") != index for index, row in enumerate(events)):
        raise ValueError("Android event ordering incomplete")
    batches = [row for row in events if row.get("kind") == "table_batch"]
    if ([row.get("args") for row in batches] != BATCHES or
            any(row.get("listCount") != 158 for row in batches) or
            len({row.get("call") for row in batches}) != 5):
        raise ValueError("Android batches incomplete")
    android_groups = {row["call"]: [] for row in batches}
    threads = {row["call"]: row["thread"] for row in batches}
    for row in events:
        if row.get("kind") in NAMES:
            if row.get("batch") not in android_groups or row.get("thread") != threads[row["batch"]]:
                raise ValueError("Android getter scope mismatch")
            android_groups[row["batch"]].append((row["kind"], row["value"] & 0xffffffff))
    if (linux.get("exit_code") != 0 or linux.get("error") or linux.get("active_scopes") != 0):
        raise ValueError("Linux observer incomplete")
    targets = {row["name"]: row for row in linux["targets"]}
    expected = {"table_batch": (5, None, "table_batch"), "entry_type": (158, "table_batch", None),
                "entry_id": (155, "table_batch", None)}
    if set(targets) != set(expected):
        raise ValueError("Linux targets incomplete")
    for name, (count, scope, start) in expected.items():
        row = targets[name]
        if (row.get("armed") is not True or row.get("calls") != count or row.get("scope") != scope or
                row.get("scope_start") != start or row.get("site_only") is not False):
            raise ValueError("Linux target coverage incomplete")
    linux_groups = []
    current_thread = None
    for row in linux["events"]:
        if "return_x0" not in row:
            raise ValueError("Linux return missing")
        if row["name"] == "table_batch":
            linux_groups.append([])
            current_thread = row["thread"]
        elif row["name"] in NAMES:
            if not linux_groups or row["thread"] != current_thread:
                raise ValueError("Linux getter scope mismatch")
            linux_groups[-1].append((row["name"], row["return_x0"] & 0xffffffff))
        else:
            raise ValueError("unexpected Linux event")
    android_groups = [android_groups[row["call"]] for row in batches]
    if len(linux_groups) != 5 or Counter(name for group in linux_groups for name, _ in group) != {
            "entry_type": 158, "entry_id": 155}:
        raise ValueError("Linux event counts differ")
    if android_groups != linux_groups:
        raise ValueError("getter sequences differ")
    return [len(group) for group in android_groups]


def audit(paths, identities):
    if any(sha(path) != identities[name] for name, path in paths.items()):
        raise ValueError("input identity mismatch")
    android = json.loads(paths["android"].read_text())
    linux_result = json.loads(paths["linux_result"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    if android.get("script_sha256") != identities["android_script"]:
        raise ValueError("Android script identity mismatch")
    if (linux_result.get("status") != "BLOCKED" or linux_result.get("exit_code") != 0 or
            linux_result.get("timed_out") is not False or linux_result.get("catalog_ready_pass") is not False or
            linux_result.get("inputs_before", {}).get("libg.so") != ENGINE or
            linux_result.get("outputs", {}).get("observation.json") != identities["linux_observation"] or
            linux_result.get("postcheck_errors") != [] or not all(linux_result.get(name) is True for name in
            ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged"))):
        raise ValueError("Linux run identity incomplete")
    for label in ("before", "after"):
        if android["update_" + label]["sha256"] != identities["android_" + label]:
            raise ValueError("Android update identity mismatch")
    before = parse_inventory(paths["android_before"])
    after = parse_inventory(paths["android_after"])
    historical = parse_inventory(paths["historical_update"])
    if before != after or any(historical.get(name) != value for name, value in before.items()):
        raise ValueError("Android update contents mismatch")
    sizes = sequences(android, linux)
    return {"schema": "nashura.catalog-entry-sequence-audit.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "batch_event_counts": sizes, "ordered_getter_values_match": True,
            "android_update_files": len(before), "historical_update_files": len(historical),
            "historical_update_missing_from_android": len(historical) - len(before), "catalog_ready_pass": False,
            "limitations": ["different processes and update inventories", "getter values do not establish row parity",
                            "unobserved paths remain unknown", "backup and configuration parity unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_script", "android_before", "android_after", "historical_update", "linux_result", "linux_observation")
    for name in names:
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
        parser.add_argument("--" + name.replace("_", "-") + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = {name: getattr(args, name) for name in names}
    identities = {name: getattr(args, name + "_sha256") for name in names}
    result = audit(paths, identities)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
