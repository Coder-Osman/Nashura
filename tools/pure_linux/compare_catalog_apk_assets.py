"""Compare historical Android and Linux APK assets without exposing resource content."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import zipfile


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def assets(archive):
    result = {}
    for entry in archive.infolist():
        if not entry.filename.startswith("assets/") or entry.is_dir():
            continue
        name = entry.filename[len("assets/"):]
        if not name or name in result or any(part in ("", ".", "..") for part in name.split("/")):
            raise ValueError("invalid or duplicate asset path")
        result[name] = entry
    return result


def asset_hash(archive, info):
    h = hashlib.sha256()
    count = 0
    with archive.open(info) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(block)
            h.update(block)
    if count != info.file_size:
        raise ValueError("asset length mismatch")
    return h.hexdigest()


def compare(stock, current, staged, opened):
    with zipfile.ZipFile(stock) as left, zipfile.ZipFile(current) as right:
        left_assets, right_assets = assets(left), assets(right)
        common = set(left_assets) & set(right_assets)
        left_only, right_only = set(left_assets) - common, set(right_assets) - common
        if not set(staged).issubset(common) or not set(opened).issubset(staged):
            raise ValueError("traced assets missing from staged inputs or APKs")
        differences = staged_differences = 0
        for name in sorted(common):
            historical = asset_hash(left, left_assets[name])
            current_hash = asset_hash(right, right_assets[name])
            differences += historical != current_hash
            if name in staged:
                staged_differences += current_hash != staged[name]
        return {"stock_assets": len(left_assets), "current_assets": len(right_assets),
                "common_assets": len(common), "stock_only": len(left_only), "current_only": len(right_only),
                "different_asset_bytes": differences, "staged_assets": len(staged),
                "staged_manifest_mismatches": staged_differences,
                "loader_asset_open_calls": sum(opened.values()), "loader_unique_asset_paths": len(opened)}


def audit(stock, current, result_path, stderr_path, reference_path, expected):
    paths = {"stock_apk": stock, "current_apk": current, "loader_result": result_path,
             "loader_stderr": stderr_path, "e1_comparison": reference_path}
    for name, path in paths.items():
        if sha(path) != expected[name]:
            raise ValueError("input identity mismatch: " + name)
    result = json.loads(result_path.read_text())
    if (result["exit_code"] != 0 or result["inputs_unchanged"] is not True or
            result["resources_unchanged"] is not True or result["postcheck_errors"] != [] or
            result["resources_before"] != result["resources_after"] or
            result["outputs"][stderr_path.name] != expected["loader_stderr"]):
        raise ValueError("loader evidence incomplete")
    if reference_path.read_text().strip() != "summary\tfiles=9829\tbytes=849482445\tfailures=0\tdescriptors=9829\tmissing=2":
        raise ValueError("E1 comparison summary differs")
    opened = Counter()
    for line in stderr_path.read_text().splitlines():
        fields = line.split("\t")
        if fields[0] == "NATIVE_ASSET_OPEN":
            if len(fields) != 3:
                raise ValueError("malformed asset open")
            opened[fields[1]] += 1
    if not opened:
        raise ValueError("loader asset opens absent")
    counts = compare(stock, current, result["resources_before"]["assets"], opened)
    clean = not any(counts[name] for name in ("stock_only", "current_only", "different_asset_bytes",
                                               "staged_manifest_mismatches"))
    return {"schema": "nashura.catalog-apk-identity.v1", "status": "PASS_LIMITED" if clean else "DIFFERENCES",
            "input_sha256": expected, "counts": counts, "catalog_ready_pass": False,
            "limitations": ["archive byte identity is not a catalog cell comparison",
                            "update resources and request-level fallback are not compared",
                            "historical Android and Linux loader ran in separate processes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("stock-apk", "current-apk", "loader-result", "loader-stderr", "e1-comparison", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("stock-apk-sha256", "current-apk-sha256", "loader-result-sha256",
                 "loader-stderr-sha256", "e1-comparison-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    names = ("stock_apk", "current_apk", "loader_result", "loader_stderr", "e1_comparison")
    report = audit(*(getattr(args, name) for name in names),
                   {name: getattr(args, name + "_sha256") for name in names})
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0 if report["status"] == "PASS_LIMITED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
