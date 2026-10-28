"""Run isolated file-routing probes against the exact original engine."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from native_init_job import digest
from provider_job import resource_inventory


APK = b"apk-only-resource\n"
APK_CONFLICT = b"apk-conflict-resource\n"
UPDATE = b"update-only-resource\n"
UPDATE_CONFLICT = b"update-conflict-resource\n"

CASES = (
    ("present", "matrix/apk-only.txt", "apk", APK),
    ("present", "data:/matrix/apk-only.txt", "apk", APK),
    ("present", "update:/matrix/apk-only.txt", "missing", None),
    ("present", "matrix/update-only.txt", "disk", UPDATE),
    ("present", "data:/matrix/update-only.txt", "missing", None),
    ("present", "update:/matrix/update-only.txt", "disk", UPDATE),
    ("present", "matrix/conflict.txt", "disk", UPDATE_CONFLICT),
    ("present", "data:/matrix/conflict.txt", "apk", APK_CONFLICT),
    ("present", "update:/matrix/conflict.txt", "disk", UPDATE_CONFLICT),
    ("present", "matrix/missing.txt", "missing", None),
    ("removed", "matrix/conflict.txt", "apk", APK_CONFLICT),
    ("removed", "data:/matrix/conflict.txt", "apk", APK_CONFLICT),
    ("removed", "update:/matrix/conflict.txt", "missing", None),
)


def create_stage(root, removed):
    assets = root / "assets"
    update = root / "update"
    for directory in (assets / "matrix", update / "matrix"):
        directory.mkdir(parents=True)
    files = [(assets / "matrix/apk-only.txt", APK), (assets / "matrix/conflict.txt", APK_CONFLICT),
             (update / "matrix/update-only.txt", UPDATE)]
    if not removed:
        files.append((update / "matrix/conflict.txt", UPDATE_CONFLICT))
    for path, content in files:
        path.write_bytes(content)
    index = root / "asset.index"
    index.write_text(f"{len(APK)}\t1\tmatrix/apk-only.txt\n"
                     f"{len(APK_CONFLICT)}\t0\tmatrix/conflict.txt\n")
    manifest = root / "resources.json"
    manifest.write_text(json.dumps(resource_inventory({"data": assets, "update": update,
                                                       "search": update, "assets": assets}), indent=2) + "\n")
    return assets, update, index, manifest


def run_case(case, ordinal, root, stage, payload, probe, backend):
    stage_name, path, expected_kind, content = case
    assets, update, index, manifest = stage
    output = root / f"case-{ordinal:02d}"
    expected_hash = hashlib.sha256(content).hexdigest() if content is not None else "0" * 64
    command = [sys.executable, str(Path(__file__).with_name("provider_job.py")),
               "--payload", str(payload), "--probe", str(probe), "--output", str(output),
               "--mode", "both", "--data-root", str(assets), "--update-root", str(update),
               "--data-path", "/", "--search-root", str(update), "--resource-manifest", str(manifest),
               "--asset-root", str(assets), "--asset-index", str(index),
               "--asset-index-sha256", digest(index), "--asset-backend", str(backend),
               "--asset-backend-sha256", digest(backend), "--resource-name", path,
               "--expected-resource-sha256", expected_hash, "--timeout", "55"]
    try:
        process = subprocess.run(command, capture_output=True, timeout=90)
        wrapper_exit, timed_out = process.returncode, False
        (root / f"wrapper-{ordinal:02d}.stdout").write_bytes(process.stdout)
        (root / f"wrapper-{ordinal:02d}.stderr").write_bytes(process.stderr)
    except subprocess.TimeoutExpired:
        return {"case": ordinal, "stage": stage_name, "path": path, "expected": expected_kind,
                "status": "TIMEOUT", "passed": False}
    result_path = output / "result.json"
    if not result_path.is_file():
        return {"case": ordinal, "stage": stage_name, "path": path, "expected": expected_kind,
                "status": "MISSING_RESULT", "wrapper_exit": wrapper_exit, "passed": False}
    result = json.loads(result_path.read_text())
    stdout = (output / "stdout.log").read_text()
    lines = stdout.splitlines()
    kinds = [line.split("\t", 1)[1] for line in lines if line.startswith("RESOURCE_FILE_KIND\t")]
    missing = [line for line in lines if line == "RESOURCE_MISSING\t1"]
    identity = (result["inputs_unchanged"] and result["resources_unchanged"] and
                result["auxiliary_unchanged"] and result["probe_unchanged"] and
                not result["postcheck_errors"] and not result["timed_out"])
    if expected_kind == "missing":
        invalid_file = (kinds == ["apk"] and "RESOURCE_INVALID_FILE\t1" in lines and
                        "PHASE\tresource_close_return" in lines)
        passed = (identity and result["exit_code"] == 60 and wrapper_exit == 1 and
                  missing == ["RESOURCE_MISSING\t1"] and (not kinds or invalid_file) and
                  "PHASE\tresource_open_return" in lines and "PROVIDER_SLOTS_RELEASED\t1" in lines and
                  "PHASE\tafter_close" in lines and "NATIVE_ASSET_FINISH_RESULT\t0" in lines and
                  not (output / "resource.bin").exists())
        actual_kind = "missing" if missing else "unknown"
        resource_hash = None
    else:
        resource_path = output / "resource.bin"
        resource_hash = digest(resource_path) if resource_path.is_file() else None
        passed = (identity and result["exit_code"] == 0 and wrapper_exit == 0 and
                  kinds == [expected_kind] and not missing and resource_hash == expected_hash and
                  result["resource_observations"]["complete"] and
                  "NATIVE_ASSET_FINISH_RESULT\t0" in lines)
        actual_kind = kinds[0] if len(kinds) == 1 else "unknown"
    return {"case": ordinal, "stage": stage_name, "path": path, "expected": expected_kind,
            "actual": actual_kind, "expected_sha256": expected_hash if content is not None else None,
            "resource_sha256": resource_hash, "native_exit": result["exit_code"],
            "wrapper_exit": wrapper_exit, "timed_out": timed_out, "passed": bool(passed),
            "result_sha256": digest(result_path), "stdout_sha256": digest(output / "stdout.log"),
            "stderr_sha256": digest(output / "stderr.log"),
            "cleanup_scope": "provider_and_asset_finish"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("payload", "probe", "backend", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    payload, probe, backend, root = (getattr(args, name).resolve() for name in ("payload", "probe", "backend", "output"))
    root.mkdir(mode=0o700, exist_ok=False)
    stages = {name: create_stage(root / name, name == "removed") for name in ("present", "removed")}
    results = []
    for ordinal, case in enumerate(CASES):
        try:
            results.append(run_case(case, ordinal, root, stages[case[0]], payload, probe, backend))
        except Exception as error:
            results.append({"case": ordinal, "stage": case[0], "path": case[1], "status": "ERROR",
                            "error": str(error), "passed": False})
    report = {"schema": "nashura.pure-linux-resource-matrix.v1", "cases": results,
              "fixture": "synthetic files, real native libg and resource providers",
              "android_matrix_comparison": "RULES_ONLY_NOT_PAIRED_BYTES", "catalog_ready_pass": False}
    with (root / "matrix.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0 if len(results) == len(CASES) and all(case["passed"] for case in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
