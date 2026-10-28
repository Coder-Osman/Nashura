"""Compare E1 update inventory with resources opened by the Linux catalog loader."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from catalog_resource_audit import audit as audit_loader
from resource_trace import analyze


RECORD = re.compile(r"([0-9a-f]{64})  \./(.+)")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_inventory(path):
    result = {}
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty update inventory")
    for row in rows:
        match = RECORD.fullmatch(row) if isinstance(row, str) else None
        if not match:
            raise ValueError("invalid update inventory record")
        digest, name = match.groups()
        if (name in result or PurePosixPath(name).is_absolute() or
                any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("invalid or duplicate update path")
        result[name] = digest
    return result


def compare(reference, linux, opened):
    left, right = set(reference), set(linux)
    if not opened or not set(opened).issubset(right):
        raise ValueError("traced update paths not bound to inventory")
    common = left & right
    return {"android_files": len(left), "linux_files": len(right),
            "android_only": len(left - right), "linux_only": len(right - left),
            "sha_mismatches": sum(reference[name] != linux[name] for name in common),
            "opened_calls": len(opened), "opened_unique_paths": len(set(opened)),
            "opened_sha_mismatches": sum(reference.get(name) != linux[name] for name in opened)}


def audit(before, after, result, trace, stderr, expected, roots, cwd):
    files = {"android_before": before, "android_after": after, "loader_result": result,
             "loader_trace": trace, "loader_stderr": stderr}
    if any(sha(path) != expected[name] for name, path in files.items()):
        raise ValueError("input identity mismatch")
    android = parse_inventory(before)
    if android != parse_inventory(after):
        raise ValueError("Android update inventory changed")
    summary = audit_loader(result, expected["loader_result"], trace, stderr, roots, cwd)
    events = analyze(trace.read_text().splitlines(), roots, cwd=cwd)
    opened = [row["path"] for row in events["records"] if row["root"] == "update"]
    if len(opened) != summary["update_open_calls"]:
        raise ValueError("trace counts differ")
    linux = json.loads(result.read_text())["resources_before"]["update"]
    counts = compare(android, linux, opened)
    clean = not any(counts[name] for name in ("android_only", "linux_only", "sha_mismatches", "opened_sha_mismatches"))
    return {"schema": "nashura.catalog-update-identity.v1", "status": "PASS_LIMITED" if clean else "DIFFERENCES",
            "input_sha256": expected, "counts": counts, "catalog_ready_pass": False,
            "limitations": ["recorded file identities do not prove mapped bytes were consumed",
                            "request-level fallback and catalog cell contents are not compared",
                            "Android and Linux runs occurred separately"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android-before", "android-after", "loader-result", "loader-trace", "loader-stderr", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("android-before-sha256", "android-after-sha256", "loader-result-sha256",
                 "loader-trace-sha256", "loader-stderr-sha256", "assets-root", "update-root", "cwd"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    names = ("android_before", "android_after", "loader_result", "loader_trace", "loader_stderr")
    report = audit(*(getattr(args, name) for name in names),
                   {name: getattr(args, name + "_sha256") for name in names},
                   {"assets": args.assets_root, "update": args.update_root}, args.cwd)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0 if report["status"] == "PASS_LIMITED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
