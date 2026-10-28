"""Audit matched update inventories and bounded catalog row-field observations."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from compare_catalog_row_fields import ENGINE, SLOTS, grouped
from compare_catalog_update_assets import parse_inventory
from parsed_tables import observations


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def linux_layout(text):
    parsed = observations(text)
    if not parsed["complete"]:
        raise ValueError("Linux layout incomplete")
    tables = [list(map(int, line.split("\t")[2:])) for line in text.splitlines()
              if line.startswith("PARSED_TABLE\tafter\t")]
    rows = [list(map(int, line.split("\t")[1:])) for line in text.splitlines()
            if line.startswith("PARSED_ROW_ID\t")]
    sizes = {row[0]: row[1] for row in tables}
    return tables, grouped(rows, sizes), parsed


def compare_layouts(android, old_text, new_text):
    old_tables, old_rows, old_parsed = linux_layout(old_text)
    new_tables, new_rows, new_parsed = linux_layout(new_text)
    if old_tables != new_tables or old_rows != new_rows:
        raise ValueError("Linux layouts or covered row fields differ")
    layout = android["layout"]
    if layout.get("schema") != "nashura.android-catalog-layout.v1" or layout.get("catalogReadyPass") is not False:
        raise ValueError("Android layout scope differs")
    android_tables = {row[0]: row for row in layout["tables"]}
    linux_tables = {row[0]: row for row in new_tables}
    if (len(android_tables) != len(layout["tables"]) or set(android_tables) != set(linux_tables) or
            sum(row[1] for row in android_tables.values()) != layout["totalRows"]):
        raise ValueError("Android table layout incomplete")
    android_rows = grouped(layout["rowIds"], {slot: row[1] for slot, row in android_tables.items()})
    differences = []
    for slot in sorted(android_tables):
        a, b = android_tables[slot], linux_tables[slot]
        if (a[1], a[3], a[4], a[5]) != (b[1], b[3], b[4], b[5]):
            differences.append(slot)
    android_only = linux_only = 0
    for slot in SLOTS:
        a, b = Counter(android_rows[slot]), Counter(new_rows[slot])
        android_only += sum((a - b).values())
        linux_only += sum((b - a).values())
    return {"linux_tables": len(new_tables), "linux_rows": new_parsed["summaries"]["after"][1],
            "android_tables": len(android_tables), "android_rows": layout["totalRows"],
            "different_slots": differences, "covered_linux_fields": sum(map(len, new_rows.values())),
            "covered_android_fields": sum(map(len, android_rows.values())),
            "covered_android_only": android_only, "covered_linux_only": linux_only,
            "linux_layout_sha256": new_parsed["layout_sha256"],
            "linux_original_layout_sha256": old_parsed["layout_sha256"]}


def audit(paths, expected):
    if any(sha(path) != expected[name] for name, path in paths.items()):
        raise ValueError("input identity mismatch")
    android = json.loads(paths["android"].read_text())
    old = json.loads(paths["old_result"].read_text())
    new = json.loads(paths["new_result"].read_text())
    if (android.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            android.get("engine_sha256") != ENGINE or android.get("script_error_count") != 0 or
            android.get("catalog_ready_pass") is not False or android.get("update_unchanged") is not True or
            android.get("script_sha256") != expected["trace_script"] or
            android.get("layout_script_sha256") != expected["layout_script"] or
            android.get("cleanup_error") or android.get("resume") != "returned" or
            android.get("spawn_gate", {}).get("disabled") is not True):
        raise ValueError("Android capture incomplete")
    trace = android["trace"]
    if (trace.get("errors") != [] or trace.get("lost") != 0 or trace.get("activeScopes") != 0 or
            trace.get("activeBatchScopes") != 0 or
            sum(row.get("kind") == "table_batch" for row in trace["events"]) != 5):
        raise ValueError("Android observation incomplete")
    for label in ("before", "after"):
        if android["update_" + label]["sha256"] != expected["android_" + label]:
            raise ValueError("Android inventory identity mismatch")
    inventory = parse_inventory(paths["android_before"])
    if inventory != parse_inventory(paths["android_after"]):
        raise ValueError("Android inventory changed")
    for name, result in (("old", old), ("new", new)):
        if (result.get("status") != "BLOCKED" or result.get("exit_code") != 0 or
                result.get("timed_out") is not False or result.get("catalog_ready_pass") is not False or
                result.get("inputs_before", {}).get("libg.so") != ENGINE or result.get("postcheck_errors") != [] or
                result.get("outputs", {}).get("stdout.log") != expected[name + "_stdout"] or
                not all(result.get(key) is True for key in
                        ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged"))):
            raise ValueError("Linux result incomplete")
    if old["inputs_before"] != new["inputs_before"] or old["probe_sha256"] != new["probe_sha256"]:
        raise ValueError("Linux engine inputs differ")
    old_resources, new_resources = old["resources_before"], new["resources_before"]
    if (old_resources["assets"] != new_resources["assets"] or old_resources["data"] != new_resources["data"] or
            old_resources["search"] != old_resources["update"] or
            new_resources["search"] != inventory or new_resources["update"] != inventory or
            any(old_resources["update"].get(name) != digest for name, digest in inventory.items())):
        raise ValueError("update subset identity differs")
    result = compare_layouts(android, paths["old_stdout"].read_text(), paths["new_stdout"].read_text())
    return {"schema": "nashura.catalog-matched-update-layout.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": expected, "android_update_files": len(inventory),
            "original_linux_update_files": len(old_resources["update"]),
            "matched_linux_update_files": len(new_resources["update"]), "layout": result,
            "catalog_ready_pass": False,
            "limitations": ["separate processes and platform runtimes", "only selected row fields compared",
                            "row field semantics not established for all types", "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_before", "android_after", "trace_script", "layout_script",
             "old_result", "old_stdout", "new_result", "new_stdout")
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
