"""Compare selected native row offset-0x40 fields without publishing identifiers."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from parsed_tables import observations


SLOTS = (9, 10, 22, 34, 35, 123, 133, 166, 173, 179, 187, 189, 191, 205, 212, 213, 214)
ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def grouped(rows, sizes):
    result = {slot: [] for slot in SLOTS}
    for slot, ordinal, value in rows:
        if slot not in result or ordinal != len(result[slot]) or not 0 <= value <= 0xffffffff:
            raise ValueError("invalid row order, slot or value")
        result[slot].append(value)
    if any(len(result[slot]) != sizes.get(slot) for slot in SLOTS):
        raise ValueError("row coverage incomplete")
    return result


def compare(android_path, linux_result_path, linux_stdout_path, expected):
    paths = {"android": android_path, "linux_result": linux_result_path, "linux_stdout": linux_stdout_path}
    if any(sha(path) != expected[name] for name, path in paths.items()):
        raise ValueError("input SHA mismatch")
    android = json.loads(android_path.read_text())
    linux = json.loads(linux_result_path.read_text())
    if (android.get("status") != "FAILED" or android.get("error") != "ValueError: update inventory after capture differs"
            or android.get("engine_sha256") != ENGINE or android.get("catalog_ready_pass") is not False):
        raise ValueError("Android scope or failed identity gate missing")
    if (linux.get("exit_code") != 0 or linux.get("status") != "BLOCKED" or linux.get("timed_out") is not False
            or linux.get("inputs_before", {}).get("libg.so") != ENGINE or not linux.get("inputs_unchanged")
            or not linux.get("resources_unchanged") or linux.get("postcheck_errors") != []
            or linux.get("catalog_ready_pass") is not False
            or linux.get("outputs", {}).get("stdout.log") != expected["linux_stdout"]):
        raise ValueError("Linux identity or native outcome differs")
    text = linux_stdout_path.read_text()
    parsed = observations(text)
    if not parsed["complete"]:
        raise ValueError("Linux table layout incomplete")
    layout = android["layout"]
    android_sizes = {row[0]: row[1] for row in layout["tables"]}
    linux_sizes = {int(row[2]): int(row[3]) for line in text.splitlines()
                   if line.startswith("PARSED_TABLE\tafter\t") for row in [line.split("\t")]}
    left = grouped(layout["rowIds"], android_sizes)
    rows = [list(map(int, line.split("\t")[1:])) for line in text.splitlines() if line.startswith("PARSED_ROW_ID\t")]
    right = grouped(rows, linux_sizes)
    result = []
    for slot in SLOTS:
        a, b = Counter(left[slot]), Counter(right[slot])
        result.append({"slot": slot, "android_count": len(left[slot]), "linux_count": len(right[slot]),
                       "android_only": sum((a - b).values()), "linux_only": sum((b - a).values()),
                       "android_duplicates": len(left[slot]) - len(a), "linux_duplicates": len(right[slot]) - len(b)})
    return {"schema": "nashura.catalog-row-field-comparison.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": expected, "slots": result, "android_count": sum(len(x) for x in left.values()),
            "linux_count": sum(len(x) for x in right.values()), "catalog_ready_pass": False,
            "limitations": ["Android update inventory changed and capture failed its strict gate",
                            "offset 0x40 semantics not established for all row types",
                            "separate runs; inclusion does not prove equal object contents"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android", "linux-result", "linux-stdout", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("android-sha256", "linux-result-sha256", "linux-stdout-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    expected = {"android": args.android_sha256, "linux_result": args.linux_result_sha256,
                "linux_stdout": args.linux_stdout_sha256}
    report = compare(args.android, args.linux_result, args.linux_stdout, expected)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
