"""Compare observed Android file-wrapper kinds with Linux resource opens."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from catalog_resource_audit import audit as audit_loader
from compare_catalog_update_assets import parse_inventory
from resource_trace import analyze


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify(android_events, linux_records, stderr):
    rows = [row for row in android_events if row.get("kind") == "filesystem_open" and
            isinstance(row.get("path"), str) and row["path"].startswith("csv_logic/")]
    android = {}
    for row in rows:
        name = row["path"]
        if name in android:
            raise ValueError("repeated logical request is ambiguous")
        table = row.get("vtable")
        kind = {"0x18c35b0": "apk", "0x18c3130": "update", None: "null"}.get(table, "unknown")
        if kind == "unknown" or row.get("nonnull") is not (kind != "null"):
            raise ValueError("unclassified Android object")
        android[name] = kind
    categories = {"apk": set(), "update": set(), "absent": set()}
    categories["update"] = {row["path"] for row in linux_records if row["root"] == "update"}
    for line in stderr.splitlines():
        fields = line.split("\t")
        if fields[0] == "NATIVE_ASSET_OPEN" and len(fields) == 3:
            categories["apk"].add(fields[1])
        elif fields[0] == "NATIVE_ASSET_ABSENT_FROM_APK" and len(fields) == 2:
            categories["absent"].add(fields[1])
    counts = Counter()
    for name, kind in android.items():
        matches = {label for label, paths in categories.items() if name in paths}
        # Missing APK probes may precede a successful update; update takes priority.
        linux = "update" if "update" in matches else "apk" if "apk" in matches else "absent" if "absent" in matches else "unknown"
        counts[(kind, linux)] += 1
    linux_names = set.union(*categories.values())
    logical_linux = {name for name in linux_names if name.startswith("csv_logic/")}
    return android, counts, len(logical_linux - android.keys())


def audit(paths, expected, roots, cwd):
    if any(sha(path) != expected[name] for name, path in paths.items()):
        raise ValueError("input identity mismatch")
    android = json.loads(paths["android"].read_text())
    trace = android["trace"]
    if (android.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            android.get("catalog_ready_pass") is not False or android.get("cleanup_error") or
            android.get("spawn_thread_alive") is not False or trace.get("errors") != [] or
            trace.get("lost") != 0 or trace.get("activeScopes") != 0):
        raise ValueError("Android observation incomplete")
    coverage = {entry["label"]: entry["installed"] for entry in trace["coverage"]}
    if not all(coverage.get(name) is True for name in ("filesystem_open", "asset_open", "table_batch")):
        raise ValueError("hook coverage incomplete")
    for phase in ("before", "after"):
        if android["update_" + phase]["sha256"] != expected["android_" + phase]:
            raise ValueError("Android update identity mismatch")
    before = parse_inventory(paths["android_before"])
    after = parse_inventory(paths["android_after"])
    audit_loader(paths["linux_result"], expected["linux_result"], paths["linux_trace"], paths["linux_stderr"], roots, cwd)
    linux_trace = analyze(paths["linux_trace"].read_text().splitlines(), roots, cwd=cwd)
    android_rows, pairs, linux_only = classify(trace["events"], linux_trace["records"], paths["linux_stderr"].read_text())
    expected_pairs = {("apk", "apk"), ("update", "update"), ("null", "absent")}
    return {"schema": "nashura.catalog-resource-selection.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": expected, "logical_requests": len(android_rows),
            "paired_counts": {a + ":" + b: count for (a, b), count in sorted(pairs.items())},
            "mismatch_count": sum(count for pair, count in pairs.items() if pair not in expected_pairs),
            "linux_only_paths": linux_only, "android_update_before": len(before), "android_update_after": len(after),
            "removed": len(set(before) - set(after)), "catalog_ready_pass": False,
            "limitations": ["APK object can be an invalid wrapper; virtual validity was not called",
                            "Android null is compared with Linux absent observation, not full causal trace",
                            "separate process executions and Android update identity changed",
                            "object type does not establish byte or parse parity"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android", "android-before", "android-after", "linux-result", "linux-trace", "linux-stderr", "identities", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("assets-root", "update-root", "cwd"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    names = ("android", "android_before", "android_after", "linux_result", "linux_trace", "linux_stderr")
    result = audit({name: getattr(args, name) for name in names}, json.loads(args.identities.read_text()),
                   {"assets": args.assets_root, "update": args.update_root}, args.cwd)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
