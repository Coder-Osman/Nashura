"""Compare bounded native catalog finish-call boundaries across runtimes."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_catalog_batch_counts import BATCHES, checked_counts
from catalog_table_counts import SLOTS
from compare_catalog_row_fields import ENGINE
from compare_catalog_update_assets import parse_inventory


TARGETS = {"table_batch": (0xe1bdf4, 5, None, "table_batch"),
           "finish_a": (0xe1c290, 1, "table_batch", None),
           "finish_b": (0xdcd238, 1, "table_batch", None)}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_phases(android, linux):
    events = android["trace"]["events"]
    starts = [row for row in events if row.get("kind") == "table_batch"]
    ends = [row for row in events if row.get("kind") == "table_batch_end"]
    phases = [row for row in events if row.get("kind") == "batch_phase"]
    if (len(starts) != 5 or len(ends) != 5 or [row.get("args") for row in starts] != BATCHES or
            any(row.get("listCount") != 158 for row in starts) or
            [row.get("call") for row in starts] != [row.get("call") for row in ends] or
            len({row.get("call") for row in starts}) != 5):
        raise ValueError("Android batch coverage incomplete")
    expected = [("finish_a", "before"), ("finish_a", "after"),
                ("finish_b", "before"), ("finish_b", "after")]
    if [(row.get("phase"), row.get("edge")) for row in phases] != expected:
        raise ValueError("Android finish phase coverage incomplete")
    if any(row.get("batch") != starts[-1]["call"] or row.get("thread") != starts[-1]["thread"] or
           not starts[-1]["sequence"] < row.get("sequence", -1) < ends[-1]["sequence"] for row in phases):
        raise ValueError("Android finish phase scope differs")
    if [row["sequence"] for row in phases] != sorted(row["sequence"] for row in phases):
        raise ValueError("Android phase ordering differs")
    if linux.get("exit_code") != 0 or linux.get("error") or linux.get("active_scopes") != 0:
        raise ValueError("Linux observer incomplete")
    targets = {row["name"]: row for row in linux["targets"]}
    if set(targets) != set(TARGETS):
        raise ValueError("Linux targets differ")
    for name, (offset, calls, scope, start) in TARGETS.items():
        row = targets[name]
        if (row.get("module") != "libg.so" or row.get("offset") != offset or row.get("calls") != calls or
                row.get("armed") is not True or row.get("site_only") is not False or
                row.get("scope") != scope or row.get("scope_start") != start or
                row.get("table_counts_register") != "x0"):
            raise ValueError("Linux target coverage differs")
    calls = linux["events"]
    if [row.get("name") for row in calls] != ["table_batch"] * 5 + ["finish_a", "finish_b"]:
        raise ValueError("Linux phase order differs")
    if any("return_x0" not in row for row in calls) or any(row["thread"] != calls[-3]["thread"] for row in calls[-2:]):
        raise ValueError("Linux phase returns or thread differ")
    boundaries = []
    for phase, edge in expected:
        a = next(row for row in phases if row["phase"] == phase and row["edge"] == edge)
        b = next(row for row in calls if row["name"] == phase)
        ac = checked_counts(a.get("tableCounts"))
        bc = checked_counts(b.get("table_counts_" + edge))
        boundaries.append({"phase": phase, "edge": edge,
                           "different_slots": [slot for slot in SLOTS if ac[slot] != bc[slot]],
                           "android_rows": sum(max(0, value) for value in ac.values()),
                           "linux_rows": sum(max(0, value) for value in bc.values())})
    a_after = checked_counts(phases[1]["tableCounts"])
    a_next = checked_counts(phases[2]["tableCounts"])
    l_after = checked_counts(calls[-2]["table_counts_after"])
    l_next = checked_counts(calls[-1]["table_counts_before"])
    if a_after != a_next or l_after != l_next:
        raise ValueError("unobserved count changes between finish phases")
    if (checked_counts(phases[-1]["tableCounts"]) != checked_counts(ends[-1]["tableCounts"]) or
            checked_counts(calls[-1]["table_counts_after"]) != checked_counts(calls[-3]["table_counts_after"])):
        raise ValueError("unobserved count changes after finish phase")
    return {"boundaries": boundaries,
            "first_divergence": next((row for row in boundaries if row["different_slots"]), None)}


def audit(paths, identities):
    if any(sha(path) != identities[name] for name, path in paths.items()):
        raise ValueError("input identity mismatch")
    android = json.loads(paths["android"].read_text())
    result = json.loads(paths["linux_result"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    trace = android["trace"]
    if (android.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            android.get("engine_sha256") != ENGINE or android.get("catalog_ready_pass") is not False or
            android.get("script_sha256") != identities["trace_script"] or
            android.get("script_error_count") != 0 or android.get("update_unchanged") is not True or
            android.get("cleanup_error") or android.get("resume") != "returned" or
            android.get("spawn_gate", {}).get("disabled") is not True or trace.get("errors") != [] or
            trace.get("lost") != 0 or trace.get("activeScopes") != 0 or trace.get("activeBatchScopes") != 0):
        raise ValueError("Android capture incomplete")
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if not all(coverage.get(name) is True for name in TARGETS):
        raise ValueError("Android hook coverage incomplete")
    for label in ("before", "after"):
        if android["update_" + label]["sha256"] != identities["android_" + label]:
            raise ValueError("Android inventory identity mismatch")
    inventory = parse_inventory(paths["android_before"])
    if inventory != parse_inventory(paths["android_after"]):
        raise ValueError("Android inventory changed")
    if (result.get("status") != "BLOCKED" or result.get("exit_code") != 0 or
            result.get("timed_out") is not False or result.get("catalog_ready_pass") is not False or
            result.get("inputs_before", {}).get("libg.so") != ENGINE or result.get("postcheck_errors") != [] or
            result.get("parsed_tables", {}).get("complete") is not True or
            result.get("outputs", {}).get("observation.json") != identities["linux_observation"] or
            result["resources_before"]["update"] != inventory or
            not all(result.get(name) is True for name in
                    ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged"))):
        raise ValueError("Linux capture incomplete")
    return {"schema": "nashura.catalog-finish-phases.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": len(inventory),
            "comparison": compare_phases(android, linux), "catalog_ready_pass": False,
            "limitations": ["counts at function boundaries, not internal causal attribution",
                            "selected table counts only", "separate processes and runtimes",
                            "configuration and Backup equivalence unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_before", "android_after", "trace_script", "linux_result", "linux_observation")
    for name in names:
        option = name.replace("_", "-")
        parser.add_argument("--" + option, type=Path, required=True)
        parser.add_argument("--" + option + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit({name: getattr(args, name) for name in names},
                   {name: getattr(args, name + "_sha256") for name in names})
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
