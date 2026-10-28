"""Summarize an identity-bound loader trace without claiming resource parity."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import posixpath

from resource_trace import analyze


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(result_path, expected_result_sha, trace_path, stderr_path, roots, cwd):
    if digest(result_path) != expected_result_sha:
        raise ValueError("result identity mismatch")
    result = json.loads(result_path.read_text())
    if (result.get("exit_code") != 0 or result.get("timed_out") is not False or
            result.get("inputs_unchanged") is not True or result.get("resources_unchanged") is not True or
            result.get("auxiliary_unchanged") is not True or result.get("postcheck_errors") != [] or
            result.get("resources_before") != result.get("resources_after")):
        raise ValueError("run or input identity incomplete")
    outputs = result.get("outputs", {})
    for path in (trace_path, stderr_path):
        if path.name not in outputs or digest(path) != outputs[path.name]:
            raise ValueError("trace identity mismatch")
    resources = result["resources_before"]
    if set(roots) != {"assets", "update"} or not all(name in resources for name in roots):
        raise ValueError("expected asset and update roots")
    paths = [posixpath.normpath(roots[name]) for name in ("assets", "update")]
    if (any(not path.startswith("/") or path == "/" for path in paths) or
            paths[0] == paths[1] or paths[0].startswith(paths[1] + "/") or
            paths[1].startswith(paths[0] + "/")):
        raise ValueError("resource roots must be distinct absolute paths")
    trace = analyze(trace_path.read_text().splitlines(), roots, cwd=cwd)
    if not trace["complete_successful_trace"] or not trace["all_open_paths_resolved"]:
        raise ValueError("trace has unresolved or incomplete calls")
    records = trace["records"]
    if any(row["path"] not in resources[row["root"]] or not row["closed"] or row["errors"] or
           row["out_of_range"] for row in records):
        raise ValueError("resource identity or lifecycle mismatch")
    asset_opens = Counter(row["path"] for row in records if row["root"] == "assets")
    native_open = Counter()
    native_close = Counter()
    native_absent = Counter()
    native_reads = Counter()
    for line in stderr_path.read_text().splitlines():
        fields = line.split("\t")
        if fields[0] in {"NATIVE_ASSET_FATAL", "NATIVE_ASSET_NOT_STAGED"}:
            raise ValueError("asset backend failure")
        if fields[0] in {"NATIVE_ASSET_OPEN", "NATIVE_ASSET_READ"}:
            if len(fields) != 3 or int(fields[2]) < 0:
                raise ValueError("malformed asset operation")
            (native_open if fields[0] == "NATIVE_ASSET_OPEN" else native_reads)[fields[1]] += 1
        elif fields[0] in {"NATIVE_ASSET_CLOSE", "NATIVE_ASSET_ABSENT_FROM_APK"}:
            if len(fields) != 2:
                raise ValueError("malformed asset operation")
            (native_close if fields[0] == "NATIVE_ASSET_CLOSE" else native_absent)[fields[1]] += 1
    if native_open != asset_opens or native_open != native_close:
        raise ValueError("asset open/close and syscall counts differ")
    updates = [row for row in records if row["root"] == "update"]
    conflicts = [row for row in updates if row["path"] in resources["assets"]]
    success = {(probe["root"], probe["path"]) for probe in trace["exists_probes"]
               if probe["result"] == "0"}
    success_before = sum(any(probe["root"] == "update" and probe["path"] == row["path"] and
                             probe["result"] == "0" and probe["line"] < row["line"]
                             for probe in trace["exists_probes"]) for row in updates)
    update_names = {row["path"] for row in updates}
    if any(("update", row["path"]) not in success for row in updates):
        raise ValueError("update open lacks a successful stat")
    return {
        "schema": "nashura.catalog-resource-observation.v1",
        "status": "RUNTIME_OBSERVED_LIMITED",
        "input_sha256": {"result": expected_result_sha, "syscalls": digest(trace_path),
                         "stderr": digest(stderr_path)},
        "roots": "caller-supplied asset and update paths; inventories bound to provider result",
        "asset_open_calls": sum(native_open.values()),
        "asset_read_calls": sum(native_reads.values()),
        "asset_close_calls": sum(native_close.values()),
        "asset_absent_calls": sum(native_absent.values()),
        "update_open_calls": len(updates),
        "update_unique_paths": len(update_names),
        "update_successful_stat_before_open": success_before,
        "update_fully_mapped": sum(row["full_file_mapped"] for row in updates),
        "update_closed": sum(row["closed"] for row in updates),
        "staged_same_name": len(set(resources["assets"]) & set(resources["update"])),
        "update_open_also_staged_as_asset": len(conflicts),
        "update_open_with_different_staged_asset_sha": sum(
            resources["update"][row["path"]] != resources["assets"][row["path"]] for row in conflicts),
        "update_paths_also_opened_as_asset": len(update_names & set(asset_opens)),
        "complete_successful_trace": trace["complete_successful_trace"],
        "unresolved_relative_paths": len(trace["unresolved_relative_paths"]),
        "limitations": ["matching path sets do not prove request-level causality",
                        "mmap does not prove CPU consumption or byte parity",
                        "caller-supplied roots and recorded inventories do not prove Android parity"],
        "android_resource_parity": "NOT_RUN", "catalog_ready_pass": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("result", "trace", "stderr", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("result-sha256", "assets-root", "update-root", "cwd"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    report = audit(args.result, args.result_sha256, args.trace, args.stderr,
                   {"assets": args.assets_root, "update": args.update_root}, args.cwd)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
