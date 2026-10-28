"""Audit two failed captures with changed update inventory, without granting parity."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath

from compare_catalog_update_assets import parse_inventory
from resource_trace import analyze


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(reference_layout, v4, v5, trace, expected, roots, cwd):
    files = {"reference_layout": reference_layout, "v4_result": v4 / "result.json",
             "v4_before": v4 / "update-before.json", "v4_after": v4 / "update-after.json",
             "v5_result": v5 / "result.json", "v5_before": v5 / "update-before.json",
             "v5_after": v5 / "update-after.json", "linux_trace": trace}
    if any(sha(path) != expected[name] for name, path in files.items()):
        raise ValueError("input identity mismatch")
    old = json.loads(reference_layout.read_text())
    attempts = [json.loads((path / "result.json").read_text()) for path in (v4, v5)]
    if old.get("status") != "RUNTIME_OBSERVED_LIMITED" or any(
            item.get("status") != "FAILED" or item.get("catalog_ready_pass") is not False or
            item.get("update_reference_sha256") != expected["v4_before"] for item in attempts):
        raise ValueError("unexpected capture status")
    if attempts[1].get("error") != "ValueError: update inventory after capture differs":
        raise ValueError("missing explicit rejection")
    before = [parse_inventory(path / "update-before.json") for path in (v4, v5)]
    after = [parse_inventory(path / "update-after.json") for path in (v4, v5)]
    if before[0] != before[1] or after[0] != after[1] or old["layout"] != attempts[1].get("layout"):
        raise ValueError("capture identities or layouts differ")
    removed = set(before[0]) - set(after[0])
    added = set(after[0]) - set(before[0])
    changed = {name for name in before[0].keys() & after[0].keys() if before[0][name] != after[0][name]}
    events = analyze(trace.read_text().splitlines(), roots, cwd=cwd)
    if not events["complete_successful_trace"] or not events["all_open_paths_resolved"]:
        raise ValueError("Linux trace incomplete")
    opened = {row["path"] for row in events["records"] if row["root"] == "update"}
    return {"schema": "nashura.catalog-update-drift.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": expected, "before_count": len(before[0]), "after_count": len(after[0]),
            "removed_count": len(removed), "added_count": len(added), "changed_count": len(changed),
            "removed_extensions": dict(sorted(Counter(PurePosixPath(name).suffix for name in removed).items())),
            "removed_also_opened_by_linux": len(removed & opened), "linux_opened_update_paths": len(opened),
            "repeated_layout_equal": True, "android_rows": old["layout"]["totalRows"],
            "catalog_ready_pass": False,
            "limitations": ["deleting process and timing unknown", "path intersection does not establish causality",
                            "Linux trace does not cover every possible request", "two Android attempts rejected by inventory gate"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference-layout", "v4", "v5", "trace", "output", "identities"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("assets-root", "update-root", "cwd"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = audit(args.reference_layout, args.v4, args.v5, args.trace, json.loads(args.identities.read_text()),
                   {"assets": args.assets_root, "update": args.update_root}, args.cwd)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
