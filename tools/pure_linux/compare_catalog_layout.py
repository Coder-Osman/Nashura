"""Compare bounded Android and Linux catalog layout observations."""

import argparse
import hashlib
import json
from pathlib import Path

from parsed_tables import observations


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(android_path, android_sha, linux_path, linux_sha):
    if sha(android_path) != android_sha or sha(linux_path) != linux_sha:
        raise ValueError("input identity mismatch")
    android = json.loads(android_path.read_text())
    if (android.get("status") != "RUNTIME_OBSERVED_LIMITED" or
            android.get("catalog_ready_pass") is not False):
        raise ValueError("Android capture incomplete")
    layout = android["layout"]
    if layout.get("schema") != "nashura.android-catalog-layout.v1" or layout.get("catalogReadyPass") is not False:
        raise ValueError("Android layout scope differs")
    rows = layout["tables"]
    android_rows = {row[0]: row for row in rows}
    if len(android_rows) != len(rows) or sum(row[1] for row in rows) != layout["totalRows"]:
        raise ValueError("Android table count mismatch")
    text = linux_path.read_text()
    parsed = observations(text)
    if not parsed["complete"]:
        raise ValueError("Linux layout incomplete")
    linux_rows = {row[0]: row for row in (list(map(int, line.split("\t")[2:])) for line in text.splitlines()
                  if line.startswith("PARSED_TABLE\tafter\t"))}
    left, right = set(android_rows), set(linux_rows)
    differences = [{"slot": slot, "android_rows": android_rows[slot][1], "linux_rows": linux_rows[slot][1],
                    "android_csv_dimensions": android_rows[slot][4:6],
                    "linux_csv_dimensions": linux_rows[slot][4:6]}
                   for slot in sorted(left & right)
                   if android_rows[slot][1] != linux_rows[slot][1] or android_rows[slot][4:6] != linux_rows[slot][4:6]]
    return {"schema": "nashura.catalog-layout-comparison.v1", "status": "DIFFERENCES" if differences or left != right else "MATCH_LIMITED",
            "android_sha256": android_sha, "linux_stdout_sha256": linux_sha,
            "android_tables": len(left), "linux_tables": len(right),
            "android_rows": layout["totalRows"], "linux_rows": parsed["summaries"]["after"][1],
            "android_only_slots": sorted(left - right), "linux_only_slots": sorted(right - left),
            "differences": differences, "catalog_ready_pass": False,
            "limitations": ["different processes and initialization environments", "row counts do not compare cell values",
                            "Android update inventory was not rechecked in this capture"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android", "linux", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("android-sha256", "linux-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = compare(args.android, args.android_sha256, args.linux, args.linux_sha256)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return 2 if result["status"] == "DIFFERENCES" else 0


if __name__ == "__main__":
    raise SystemExit(main())
