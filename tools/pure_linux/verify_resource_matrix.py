"""Independently recheck a bounded native resource-matrix archive."""

import argparse
import hashlib
import json
from pathlib import Path

from native_init_job import digest
from run_resource_matrix import CASES


ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root, expected_matrix, expected_probe, expected_backend):
    matrix_path = root / "matrix.json"
    require(digest(matrix_path) == expected_matrix, "matrix identity")
    document = json.loads(matrix_path.read_text())
    require(document.get("schema") == "nashura.pure-linux-resource-matrix.v1" and
            document.get("catalog_ready_pass") is False and len(document.get("cases", [])) == len(CASES),
            "matrix scope")
    baseline_inputs = None
    for index, (stage, name, kind, content) in enumerate(CASES):
        row = document["cases"][index]
        require(row.get("case") == index and row.get("stage") == stage and row.get("path") == name and
                row.get("expected") == row.get("actual") == kind and row.get("passed") is True,
                "case identity or result")
        folder = root / f"case-{index:02d}"
        result_path = folder / "result.json"
        require(digest(result_path) == row["result_sha256"], "result identity")
        record = json.loads(result_path.read_text())
        require(record["inputs_before"].get("libg.so") == ENGINE and
                record["inputs_before"] == record["inputs_after"] and
                record["inputs_unchanged"] is True and record["resources_unchanged"] is True and
                record["auxiliary_unchanged"] is True and record["probe_unchanged"] is True and
                record["postcheck_errors"] == [] and record["timed_out"] is False and
                record["probe_sha256"] == expected_probe and
                record["auxiliary_before"]["asset_backend"] == expected_backend,
                "engine, probe or backend identity")
        if baseline_inputs is None:
            baseline_inputs = record["inputs_before"]
        require(record["inputs_before"] == baseline_inputs, "runtime identity differs between cases")
        for name_key in ("stdout", "stderr"):
            name_path = folder / (name_key + ".log")
            require(digest(name_path) == row[name_key + "_sha256"] == record["outputs"][name_path.name],
                    "log identity")
        lines = (folder / "stdout.log").read_text().splitlines()
        if kind == "missing":
            require(row["native_exit"] == record["exit_code"] == 60 and row["wrapper_exit"] == 1 and
                    lines.count("RESOURCE_MISSING\t1") == 1 and
                    row["cleanup_scope"] == "provider_and_asset_finish" and
                    "PROVIDER_SLOTS_RELEASED\t1" in lines and "PHASE\tafter_close" in lines and
                    "NATIVE_ASSET_FINISH_RESULT\t0" in lines and not (folder / "resource.bin").exists(),
                    "missing case evidence")
            if "RESOURCE_INVALID_FILE\t1" in lines:
                require("PHASE\tresource_close_return" in lines, "invalid file not closed")
        else:
            wanted = hashlib.sha256(content).hexdigest()
            resource = folder / "resource.bin"
            require(row["native_exit"] == record["exit_code"] == 0 and row["wrapper_exit"] == 0 and
                    row["expected_sha256"] == row["resource_sha256"] == wanted == digest(resource) and
                    record["resource_observations"]["complete"] is True and
                    lines.count("RESOURCE_FILE_KIND\t" + kind) == 1 and
                    "NATIVE_ASSET_FINISH_RESULT\t0" in lines and
                    row["cleanup_scope"] == "provider_and_asset_finish", "file case evidence")
    return {"schema": "nashura.pure-linux-resource-matrix-verification.v1", "status": "PASS_LIMITED",
            "cases": len(CASES), "matrix_sha256": expected_matrix, "probe_sha256": expected_probe,
            "backend_sha256": expected_backend, "libg_sha256": ENGINE,
            "scope": "synthetic routing fixtures using actual libg; not paired Android bytes",
            "catalog_ready_pass": False, "full_battle": "NOT_RUN"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--matrix-sha256", required=True)
    parser.add_argument("--probe-sha256", required=True)
    parser.add_argument("--backend-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.root, args.matrix_sha256, args.probe_sha256, args.backend_sha256)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
