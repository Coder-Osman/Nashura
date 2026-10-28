"""Replay a SHA-bound Android E1 resource matrix against Linux libg."""

import argparse
import hashlib
import json
from pathlib import Path

from native_init_job import digest
from provider_job import resource_inventory
from run_resource_matrix import run_case


ANDROID_STOCK_SHA = "5548776e26db3c4a79235358f0e097d92cd5ea6fac7cfb78ab4a6ab2e05830d5"
FIXTURE = "e1-fixture-20261026-b6e71"
EXPECTED = (
    ("apk_only_or_missing", "build.tag", "apk"),
    ("apk_only_or_missing", "data:/build.tag", "apk"),
    ("apk_only_or_missing", "update:/build.tag", "missing"),
    ("apk_only_or_missing", "image/icons/Battlefy_logo.png", "apk"),
    ("apk_only_or_missing", "data:/image/icons/Battlefy_logo.png", "apk"),
    ("apk_only_or_missing", FIXTURE + "/missing", "missing"),
    ("apk_only_or_missing", "data:/" + FIXTURE + "/missing", "missing"),
    ("apk_only_or_missing", "update:/" + FIXTURE + "/missing", "missing"),
    ("update_present", "build.tag", "disk"),
    ("update_present", "data:/build.tag", "apk"),
    ("update_present", "update:/build.tag", "disk"),
    ("update_present", FIXTURE + "/only.txt", "disk"),
    ("update_present", "data:/" + FIXTURE + "/only.txt", "missing"),
    ("update_present", "update:/" + FIXTURE + "/only.txt", "disk"),
    ("update_present", FIXTURE + "/other/build.tag", "disk"),
    ("update_present", "data:/" + FIXTURE + "/other/build.tag", "missing"),
    ("update_present", FIXTURE + "/malformed.csv", "disk"),
    ("update_present", "data:/./build.tag", "apk"),
    ("update_present", "data:/../build.tag", "missing"),
    ("update_present", "data://build.tag", "apk"),
    ("update_present", "unknown:/build.tag", "missing"),
    ("update_removed_fallback", "build.tag", "apk"),
    ("update_removed_fallback", "data:/build.tag", "apk"),
    ("update_removed_fallback", "update:/build.tag", "missing"),
)


def read_stock(path):
    if digest(path) != ANDROID_STOCK_SHA:
        raise ValueError("Android stock matrix identity mismatch")
    rows = json.loads(path.read_text())
    if len(rows) != len(EXPECTED):
        raise ValueError("Android stock matrix size")
    for row, (stage, name, kind) in zip(rows, EXPECTED):
        result = row["result"]
        if row["stage"] != stage or result["path"] != name or len(result["operations"]) != 24:
            raise ValueError("Android stock case identity")
        data = result["bytes"]
        if kind == "missing":
            if data is not None or result["read_result"] != -1 or result["operation_status"] != -1:
                raise ValueError("Android missing case differs")
        elif not isinstance(data, list) or len(data) != result["read_result"] or result["operation_status"] != 0:
            raise ValueError("Android byte or operation evidence missing")
    return rows


def create_stage(root, name, rows):
    assets, update = root / "assets", root / "update"
    assets.mkdir(parents=True)
    update.mkdir()
    files = [(assets / "build.tag", bytes(rows[0]["result"]["bytes"])),
             (assets / "image/icons/Battlefy_logo.png", bytes(rows[3]["result"]["bytes"]))]
    if name != "apk_only_or_missing":
        files += [(update / (FIXTURE + "/only.txt"), bytes(rows[11]["result"]["bytes"])),
                  (update / (FIXTURE + "/other/build.tag"), bytes(rows[14]["result"]["bytes"])),
                  (update / (FIXTURE + "/malformed.csv"), bytes(rows[16]["result"]["bytes"]))]
    if name == "update_present":
        files.append((update / "build.tag", bytes(rows[8]["result"]["bytes"])))
    for path, content in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    index = root / "asset.index"
    index.write_text(f"{len(files[0][1])}\t1\tbuild.tag\n"
                     f"{len(files[1][1])}\t0\timage/icons/Battlefy_logo.png\n")
    manifest = root / "resources.json"
    manifest.write_text(json.dumps(resource_inventory({"data": assets, "update": update,
                                                       "search": update, "assets": assets}), indent=2) + "\n")
    return assets, update, index, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("stock", "payload", "probe", "backend", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    stock, payload, probe, backend, root = (getattr(args, name).resolve()
                                             for name in ("stock", "payload", "probe", "backend", "output"))
    rows = read_stock(stock)
    root.mkdir(mode=0o700, exist_ok=False)
    stages = {name: create_stage(root / name, name, rows) for name in
              ("apk_only_or_missing", "update_present", "update_removed_fallback")}
    records = []
    for ordinal, ((stage, name, kind), android) in enumerate(zip(EXPECTED, rows)):
        expected = android["result"]
        content = bytes(expected["bytes"]) if kind != "missing" else None
        try:
            linux = run_case((stage, name, kind, content), ordinal, root, stages[stage], payload, probe,
                             backend, exercise=True)
            android_ops = [int(value) for value in expected["operations"]]
            linux["android_operation_status"] = expected["operation_status"]
            linux["operations_match_android"] = linux.get("exercise_values") == android_ops
            linux["operation_status_matches_android"] = linux.get("exercise_status") == expected["operation_status"]
            linux["android_byte_sha256"] = hashlib.sha256(content).hexdigest() if content is not None else None
            linux["paired_pass"] = bool(linux.get("passed") and linux["operations_match_android"] and
                                       linux["operation_status_matches_android"])
            records.append(linux)
        except Exception as error:
            records.append({"case": ordinal, "stage": stage, "path": name, "status": "ERROR",
                            "error": str(error), "paired_pass": False})
    report = {"schema": "nashura.pure-linux-e1-paired-resource-matrix.v1", "stock_sha256": ANDROID_STOCK_SHA,
              "cases": records, "catalog_ready_pass": False, "scope": "24 diagnostic cases; not battle parity"}
    with (root / "paired-matrix.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0 if len(records) == len(EXPECTED) and all(row["paired_pass"] for row in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
