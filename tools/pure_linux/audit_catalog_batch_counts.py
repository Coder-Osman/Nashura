"""Locate bounded per-batch catalog count divergence across real runtimes."""

import argparse
import hashlib
import json
from pathlib import Path

from catalog_table_counts import SLOTS
from compare_catalog_row_fields import ENGINE
from compare_catalog_update_assets import parse_inventory
from parsed_tables import observations


BATCHES = [[0, 0, 0, start, 39] for start in (0, 39, 78, 117, 156)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_counts(rows):
    if (not isinstance(rows, list) or len(rows) != len(SLOTS) or
            any(not isinstance(row, list) or len(row) != 2 or row[0] != slot or
                type(row[1]) is not int or row[1] < -1 or row[1] > 1000000
                for row, slot in zip(rows, SLOTS))):
        raise ValueError("invalid table count snapshot")
    return dict(rows)


def compare_boundaries(android, linux, linux_text):
    trace = android["trace"]
    starts = [row for row in trace["events"] if row.get("kind") == "table_batch"]
    ends = [row for row in trace["events"] if row.get("kind") == "table_batch_end"]
    if (len(starts) != 5 or len(ends) != 5 or [row.get("args") for row in starts] != BATCHES or
            any(row.get("listCount") != 158 for row in starts)):
        raise ValueError("Android batch coverage incomplete")
    if len({row.get("call") for row in starts}) != 5 or [row.get("call") for row in starts] != [row.get("call") for row in ends]:
        raise ValueError("Android batch association incomplete")
    if (linux.get("exit_code") != 0 or linux.get("error") or linux.get("active_scopes") != 0 or
            len(linux.get("targets", [])) != 1 or linux["targets"][0].get("calls") != 5 or
            linux["targets"][0].get("armed") is not True or
            linux["targets"][0].get("module") != "libg.so" or linux["targets"][0].get("offset") != 0xe1bdf4 or
            linux["targets"][0].get("site_only") is not False or
            linux["targets"][0].get("table_counts_register") != "x0"):
        raise ValueError("Linux batch observation incomplete")
    events = linux["events"]
    if len(events) != 5 or any(event.get("name") != "table_batch" or "return_x0" not in event for event in events):
        raise ValueError("Linux batch returns incomplete")
    boundaries = []
    previous_android = previous_linux = None
    for index, (start, end, event) in enumerate(zip(starts, ends, events)):
        if start["thread"] != end["thread"] or start["sequence"] >= end["sequence"]:
            raise ValueError("Android batch boundary ordering differs")
        if index and ends[index - 1]["sequence"] >= start["sequence"]:
            raise ValueError("overlapping Android batches")
        for phase, a, b in (("before", start.get("tableCounts"), event.get("table_counts_before")),
                            ("after", end.get("tableCounts"), event.get("table_counts_after"))):
            a, b = checked_counts(a), checked_counts(b)
            if phase == "before" and previous_android is not None and (a != previous_android or b != previous_linux):
                raise ValueError("unobserved count changes between batches")
            boundaries.append({"batch": index, "phase": phase,
                               "different_slots": [slot for slot in SLOTS if a[slot] != b[slot]],
                               "android_rows": sum(max(0, value) for value in a.values()),
                               "linux_rows": sum(max(0, value) for value in b.values())})
            if phase == "after":
                previous_android, previous_linux = a, b
    parsed = observations(linux_text)
    if not parsed["complete"]:
        raise ValueError("Linux final layout incomplete")
    linux_tables = {int(fields[2]): int(fields[3]) for line in linux_text.splitlines()
                    if line.startswith("PARSED_TABLE\tafter\t") for fields in [line.split("\t")]}
    layout = android["layout"]
    android_tables = {row[0]: row[1] for row in layout["tables"]}
    if layout.get("catalogReadyPass") is not False or set(SLOTS) - android_tables.keys() or set(SLOTS) - linux_tables.keys():
        raise ValueError("final table coverage incomplete")
    final_android = sum(android_tables[slot] for slot in SLOTS)
    final_linux = sum(linux_tables[slot] for slot in SLOTS)
    first = next((row for row in boundaries if row["different_slots"]), None)
    return {"boundaries": boundaries, "first_divergence": first,
            "final_android_rows": final_android, "final_linux_rows": final_linux,
            "post_batch_android_delta": final_android - boundaries[-1]["android_rows"],
            "post_batch_linux_delta": final_linux - boundaries[-1]["linux_rows"]}


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
            android.get("layout_script_sha256") != identities["layout_script"] or
            android.get("script_error_count") != 0 or android.get("update_unchanged") is not True or
            android.get("cleanup_error") or android.get("resume") != "returned" or
            android.get("spawn_gate", {}).get("disabled") is not True or trace.get("errors") != [] or
            trace.get("lost") != 0 or trace.get("activeScopes") != 0 or trace.get("activeBatchScopes") != 0):
        raise ValueError("Android capture incomplete")
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if coverage.get("table_batch") is not True:
        raise ValueError("Android batch hook unavailable")
    for label in ("before", "after"):
        if android["update_" + label]["sha256"] != identities["android_" + label]:
            raise ValueError("Android inventory identity mismatch")
    inventory = parse_inventory(paths["android_before"])
    if inventory != parse_inventory(paths["android_after"]):
        raise ValueError("Android inventory changed")
    if (result.get("status") != "BLOCKED" or result.get("exit_code") != 0 or
            result.get("timed_out") is not False or result.get("catalog_ready_pass") is not False or
            result.get("inputs_before", {}).get("libg.so") != ENGINE or result.get("postcheck_errors") != [] or
            result.get("outputs", {}).get("observation.json") != identities["linux_observation"] or
            result.get("outputs", {}).get("stdout.log") != identities["linux_stdout"] or
            result["resources_before"]["update"] != inventory or
            not all(result.get(name) is True for name in
                    ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged"))):
        raise ValueError("Linux capture incomplete")
    comparison = compare_boundaries(android, linux, paths["linux_stdout"].read_text())
    return {"schema": "nashura.catalog-batch-counts.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": len(inventory), "comparison": comparison,
            "catalog_ready_pass": False,
            "limitations": ["selected table counts only", "separate processes and platform runtimes",
                            "batch boundaries do not identify the responsible internal call",
                            "configuration and Backup equivalence unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_before", "android_after", "trace_script", "layout_script",
             "linux_result", "linux_observation", "linux_stdout")
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
