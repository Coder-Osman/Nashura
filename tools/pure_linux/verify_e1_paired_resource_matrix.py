"""Recheck Android E1 and native Linux resource results from private archives."""

import argparse
import hashlib
import json
from pathlib import Path

from native_init_job import digest
from run_e1_paired_resource_matrix import ANDROID_STOCK_SHA, EXPECTED, FIXTURE, read_stock
from verify_resource_matrix import ENGINE


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root, stock_path, matrix_sha, probe_sha, backend_sha):
    stock = read_stock(stock_path)
    matrix_path = root / "paired-matrix.json"
    require(digest(matrix_path) == matrix_sha, "paired matrix identity")
    report = json.loads(matrix_path.read_text())
    require(report.get("schema") == "nashura.pure-linux-e1-paired-resource-matrix.v1" and
            report.get("stock_sha256") == ANDROID_STOCK_SHA and report.get("catalog_ready_pass") is False and
            len(report.get("cases", [])) == len(EXPECTED), "paired matrix scope")
    runtime = None
    for index, ((stage, name, kind), android) in enumerate(zip(EXPECTED, stock)):
        expected = android["result"]
        row = report["cases"][index]
        require(row.get("case") == index and row.get("stage") == stage and row.get("path") == name and
                row.get("expected") == row.get("actual") == kind and row.get("paired_pass") is True,
                "case identity")
        folder = root / f"case-{index:02d}"
        result_path = folder / "result.json"
        require(digest(result_path) == row["result_sha256"], "result identity")
        result = json.loads(result_path.read_text())
        require(result["inputs_before"].get("libg.so") == ENGINE and
                result["inputs_before"] == result["inputs_after"] and
                result["inputs_unchanged"] is True and result["resources_unchanged"] is True and
                result["resources_before"] == result["resources_after"] and
                result["auxiliary_unchanged"] is True and result["probe_unchanged"] is True and
                result["postcheck_errors"] == [] and result["timed_out"] is False and
                result["probe_sha256"] == probe_sha and
                result["auxiliary_before"]["asset_backend"] == backend_sha and
                result["backend_binding_verified"] is True, "engine or resource identity")
        if runtime is None:
            runtime = result["inputs_before"]
        require(result["inputs_before"] == runtime, "runtime differs between cases")
        for label in ("stdout", "stderr"):
            log = folder / (label + ".log")
            require(digest(log) == row[label + "_sha256"] == result["outputs"][log.name], "log identity")
        lines = (folder / "stdout.log").read_text().splitlines()
        exercises = [line.split("\t")[1:] for line in lines if line.startswith("RESOURCE_EXERCISE\t")]
        require(len(exercises) == 1 and len(exercises[0]) == 25, "exercise coverage")
        exercise = list(map(int, exercises[0]))
        require(exercise[0] == expected["operation_status"] and
                exercise[1:] == [int(value) for value in expected["operations"]], "Android operations differ")
        require(any(line.startswith("PHASE\tresource_open_enter\t" + name + "\tTID\t") for line in lines) and
                "PROVIDER_SLOTS_RELEASED\t1" in lines and "PHASE\tafter_close" in lines and
                "NATIVE_ASSET_FINISH_RESULT\t0" in lines, "open or cleanup missing")
        if kind == "missing":
            require(result["exit_code"] == row["native_exit"] == 60 and row["wrapper_exit"] == 1 and
                    lines.count("RESOURCE_MISSING\t1") == 1 and not (folder / "resource.bin").exists(),
                    "missing behavior differs")
            if "RESOURCE_INVALID_FILE\t1" in lines:
                require("PHASE\tresource_close_return" in lines, "invalid wrapper not closed")
        else:
            content = bytes(expected["bytes"])
            wanted = hashlib.sha256(content).hexdigest()
            output = folder / "resource.bin"
            require(result["exit_code"] == row["native_exit"] == 0 and row["wrapper_exit"] == 0 and
                    output.read_bytes() == content and digest(output) == wanted == row["resource_sha256"] and
                    result["resource_observations"]["complete"] is True and
                    lines.count("RESOURCE_FILE_KIND\t" + kind) == 1, "resource bytes or kind differ")
        stage_root = root / stage
        require(digest(stage_root / "asset.index") == result["auxiliary_before"]["asset_index"] and
                digest(stage_root / "resources.json") == result["auxiliary_before"]["resource_manifest"],
                "stage index or manifest differs")
    return {"schema": "nashura.pure-linux-e1-paired-resource-verification.v1", "status": "PASS_LIMITED",
            "cases": len(EXPECTED), "android_stock_sha256": ANDROID_STOCK_SHA, "matrix_sha256": matrix_sha,
            "libg_sha256": ENGINE, "probe_sha256": probe_sha, "backend_sha256": backend_sha,
            "matched": "bytes, return status and 24 file operation results",
            "limitations": ["historical Android stock and current Linux runs are separate processes",
                            "synthetic update files and two APK assets only", "FMOD and catalog not exercised"],
            "catalog_ready_pass": False, "full_battle": "NOT_RUN"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "stock", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("matrix-sha256", "probe-sha256", "backend-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    report = verify(args.root, args.stock, args.matrix_sha256, args.probe_sha256, args.backend_sha256)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
