"""Audit the bounded context and lookup observations from two native runs."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_catalog_batch_counts import checked_counts
from compare_catalog_row_fields import ENGINE
from compare_catalog_update_assets import parse_inventory


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(android, linux):
    trace = android["trace"]
    starts = [event for event in trace["events"] if event.get("kind") == "table_batch"]
    ends = [event for event in trace["events"] if event.get("kind") == "table_batch_end"]
    phases = [event for event in trace["events"] if event.get("kind") == "batch_phase" and
              event.get("phase") == "finish_a"]
    lookup = [event for event in trace["events"] if event.get("kind") == "finish_lookup"]
    if len(starts) != 5 or len(ends) != 5 or [x.get("edge") for x in phases] != ["before", "after"] or len(lookup) != 1:
        raise ValueError("Android phase coverage incomplete")
    batch = starts[-1]["call"]
    if any(row.get("batch") != batch or row.get("thread") != starts[-1]["thread"] for row in phases + lookup):
        raise ValueError("Android observation scope mismatch")
    if not starts[-1]["sequence"] < phases[0]["sequence"] < lookup[0]["sequence"] < phases[1]["sequence"] < ends[-1]["sequence"]:
        raise ValueError("Android observation order mismatch")
    if type(phases[0].get("contextSlotB60Nonnull")) is not bool or type(lookup[0].get("nonnull")) is not bool:
        raise ValueError("Android observation missing")
    targets = {row["name"]: row for row in linux["targets"]}
    expected = {"table_batch": (0xe1bdf4, 5, None, "table_batch"),
                "finish_a": (0xe1c290, 1, "table_batch", "finish_a"),
                "finish_lookup": (0x1207ffc, 1, "finish_a", None)}
    if set(targets) != set(expected) or linux.get("exit_code") != 0 or linux.get("error") or linux.get("active_scopes") != 0:
        raise ValueError("Linux observer incomplete")
    for name, (offset, calls, scope, start) in expected.items():
        row = targets[name]
        if (row.get("module") != "libg.so" or row.get("offset") != offset or row.get("calls") != calls or
                row.get("scope") != scope or row.get("scope_start") != start or row.get("armed") is not True or
                row.get("site_only") is not False):
            raise ValueError("Linux target coverage mismatch")
    if targets["finish_a"].get("context_slot_b60") is not True:
        raise ValueError("Linux context observation not enabled")
    events = linux["events"]
    if [row.get("name") for row in events] != ["table_batch"] * 5 + ["finish_a", "finish_lookup"]:
        raise ValueError("Linux observation sequence mismatch")
    finish, query = events[-2:]
    if ("return_x0" not in finish or "return_x0" not in query or finish["thread"] != query["thread"] or
            type(finish.get("context_slot_b60_nonnull")) is not bool):
        raise ValueError("Linux observation missing")
    before_a, before_l = checked_counts(phases[0]["tableCounts"]), checked_counts(finish["table_counts_before"])
    after_a, after_l = checked_counts(phases[1]["tableCounts"]), checked_counts(finish["table_counts_after"])
    return {"android_context_nonnull": phases[0]["contextSlotB60Nonnull"],
            "linux_context_nonnull": finish["context_slot_b60_nonnull"],
            "android_lookup_nonnull": lookup[0]["nonnull"], "linux_lookup_nonnull": query["return_x0"] != 0,
            "before_counts_match": before_a == before_l,
            "android_rows_before": sum(max(0, x) for x in before_a.values()),
            "linux_rows_before": sum(max(0, x) for x in before_l.values()),
            "android_rows_after": sum(max(0, x) for x in after_a.values()),
            "linux_rows_after": sum(max(0, x) for x in after_l.values())}


def audit(paths, identities):
    if any(sha(path) != identities[name] for name, path in paths.items()):
        raise ValueError("input identity mismatch")
    android = json.loads(paths["android"].read_text())
    result = json.loads(paths["linux_result"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    trace = android["trace"]
    if (android.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            android.get("engine_sha256") != ENGINE or android.get("script_sha256") != identities["trace_script"] or
            android.get("catalog_ready_pass") is not False or android.get("script_error_count") != 0 or
            android.get("update_unchanged") is not True or android.get("cleanup_error") or
            android.get("resume") != "returned" or android.get("spawn_gate", {}).get("disabled") is not True or
            trace.get("errors") != [] or trace.get("lost") != 0 or trace.get("activeScopes") != 0 or
            trace.get("activeBatchScopes") != 0 or trace.get("activeFinishScopes") != 0):
        raise ValueError("Android capture incomplete")
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if not all(coverage.get(name) is True for name in ("table_batch", "finish_a", "finish_lookup")):
        raise ValueError("Android hook coverage incomplete")
    for label in ("before", "after"):
        if android["update_" + label]["sha256"] != identities["android_" + label]:
            raise ValueError("Android inventory identity mismatch")
    inventory = parse_inventory(paths["android_before"])
    if inventory != parse_inventory(paths["android_after"]):
        raise ValueError("Android inventory changed")
    if (result.get("status") != "BLOCKED" or result.get("exit_code") != 0 or result.get("timed_out") is not False or
            result.get("catalog_ready_pass") is not False or result.get("inputs_before", {}).get("libg.so") != ENGINE or
            result.get("postcheck_errors") != [] or result.get("parsed_tables", {}).get("complete") is not True or
            result.get("outputs", {}).get("observation.json") != identities["linux_observation"] or
            result["resources_before"]["update"] != inventory or not all(result.get(name) is True for name in
            ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged"))):
        raise ValueError("Linux capture incomplete")
    return {"schema": "nashura.finish-lookup.v1", "status": "RUNTIME_OBSERVED_LIMITED", "input_sha256": identities,
            "matched_update_files": len(inventory), "comparison": compare(android, linux), "catalog_ready_pass": False,
            "limitations": ["pointer identity and pointed-to contents not compared", "query key and initialization source unknown",
                            "function return difference alone does not prove causation", "separate runtimes and processes"]}


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
