"""Audit bounded startup parameters and logical resource-request sets."""

import argparse
import hashlib
import json
from pathlib import Path

from catalog_resource_audit import audit as audit_loader
from compare_catalog_update_assets import parse_inventory
from resource_trace import analyze


ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
BATCHES = [[0, 0, 0, start, 39] for start in (0, 39, 78, 117, 156)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(paths, identities, roots, cwd):
    if any(sha(path) != identities[name] for name, path in paths.items()):
        raise ValueError("input identity mismatch")
    android = json.loads(paths["android"].read_text())
    linux = json.loads(paths["linux_batch"].read_text())
    gdb = json.loads(paths["linux_gdb"].read_text())
    trace = android["trace"]
    if (android.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            android.get("engine_sha256") != ENGINE or android.get("catalog_ready_pass") is not False or
            android.get("update_unchanged") is not True or android.get("cleanup_error") or
            android.get("spawn_thread_alive") is not False or trace.get("errors") != [] or trace.get("lost") != 0):
        raise ValueError("Android capture incomplete")
    for label, name in (("before", "android_before"), ("after", "android_after")):
        if android["update_" + label]["sha256"] != identities[name]:
            raise ValueError("update inventory identity differs")
    before = parse_inventory(paths["android_before"])
    after = parse_inventory(paths["android_after"])
    reference = parse_inventory(paths["historical_update"])
    if before != after or any(reference.get(name) != digest for name, digest in before.items()):
        raise ValueError("Android update content mismatch")
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if not all(coverage.get(name) is True for name in ("table_batch", "filesystem_open", "asset_open")):
        raise ValueError("Android hook coverage incomplete")
    events = trace["events"]
    if not events or events[0].get("kind") != "engine_hooks_ready":
        raise ValueError("initial hook marker absent")
    batches = [row for row in events if row["kind"] == "table_batch"]
    if [row["args"] for row in batches] != BATCHES or any(row.get("listCount") != 158 for row in batches):
        raise ValueError("Android batch sequence differs")
    if (linux.get("exit_code") != 0 or linux.get("status") != "BLOCKED" or
            linux.get("catalog_ready_pass") is not False or linux.get("inputs_before", {}).get("libg.so") != ENGINE or
            not all(linux.get(name) is True for name in ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged")) or
            linux.get("postcheck_errors") != [] or linux.get("outputs", {}).get("observation.json") != identities["linux_gdb"]):
        raise ValueError("Linux batch result incomplete")
    targets = gdb.get("targets", [])
    calls = [row for row in gdb.get("events", []) if row.get("name") == "loader_batch_call_site"]
    if (gdb.get("exit_code") != 0 or gdb.get("error") or len(targets) != 1 or
            targets[0].get("armed") is not True or targets[0].get("site_only") is not True or
            targets[0].get("calls") != 5 or [row["arguments"][2:7] for row in calls] != BATCHES):
        raise ValueError("Linux batch observations incomplete")
    audit_loader(paths["linux_resource_result"], identities["linux_resource_result"], paths["linux_trace"],
                 paths["linux_stderr"], roots, cwd)
    resource_trace = analyze(paths["linux_trace"].read_text().splitlines(), roots, cwd=cwd)
    linux_requests = {line.split("\t")[1] for line in paths["linux_stderr"].read_text().splitlines()
                      if line.startswith(("NATIVE_ASSET_OPEN\t", "NATIVE_ASSET_ABSENT_FROM_APK\t"))
                      and line.split("\t")[1].startswith("csv_logic/")}
    linux_requests |= {row["path"] for row in resource_trace["records"]
                       if row["root"] == "update" and row["path"].startswith("csv_logic/")}
    opens = [row for row in events if row["kind"] == "filesystem_open"]
    if any(row.get("path") is None for row in opens):
        raise ValueError("unresolved Android logical path")
    android_requests = {row["path"] for row in opens if row["path"].startswith("csv_logic/")}
    return {"schema": "nashura.catalog-start-trace-audit.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "android_batch_calls": len(batches), "linux_batch_calls": len(calls),
            "batch_arguments_match": True, "android_list_count": 158,
            "android_update_files": len(before), "historical_update_files": len(reference),
            "android_csv_logic_requests": len(android_requests), "linux_csv_logic_requests": len(linux_requests),
            "android_only_paths": len(android_requests - linux_requests),
            "linux_only_paths": len(linux_requests - android_requests), "catalog_ready_pass": False,
            "limitations": ["path set is not per-request content or outcome parity",
                            "Android update subset differs from historical Linux input",
                            "unobserved earlier calls and indirect targets remain unknown",
                            "batch parameters do not prove matching catalog rows"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android", "android-before", "android-after", "historical-update", "linux-batch",
                 "linux-gdb", "linux-resource-result", "linux-trace", "linux-stderr", "identities", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("assets-root", "update-root", "cwd"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    names = ("android", "android_before", "android_after", "historical_update", "linux_batch", "linux_gdb",
             "linux_resource_result", "linux_trace", "linux_stderr")
    result = audit({name: getattr(args, name) for name in names}, json.loads(args.identities.read_text()),
                   {"assets": args.assets_root, "update": args.update_root}, args.cwd)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
