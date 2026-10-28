import hashlib
import json

import pytest

from run_resource_matrix import CASES
from verify_resource_matrix import ENGINE, verify


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root):
    rows = []
    for index, (stage, name, kind, content) in enumerate(CASES):
        folder = root / f"case-{index:02d}"
        folder.mkdir()
        missing = kind == "missing"
        lines = ["RESOURCE_MISSING\t1", "PROVIDER_SLOTS_RELEASED\t1", "PHASE\tafter_close",
                 "NATIVE_ASSET_FINISH_RESULT\t0"] if missing else ["RESOURCE_FILE_KIND\t" + kind,
                                                          "NATIVE_ASSET_FINISH_RESULT\t0"]
        if index == 4:
            lines += ["RESOURCE_INVALID_FILE\t1", "PHASE\tresource_close_return"]
        (folder / "stdout.log").write_text("\n".join(lines))
        (folder / "stderr.log").write_text("")
        h = None
        if not missing:
            (folder / "resource.bin").write_bytes(content)
            h = sha(folder / "resource.bin")
        result = {"inputs_before": {"libg.so": ENGINE}, "inputs_after": {"libg.so": ENGINE},
                  "inputs_unchanged": True, "resources_unchanged": True, "auxiliary_unchanged": True,
                  "probe_unchanged": True, "postcheck_errors": [], "timed_out": False,
                  "probe_sha256": "probe", "auxiliary_before": {"asset_backend": "backend"},
                  "exit_code": 60 if missing else 0,
                  "outputs": {"stdout.log": sha(folder / "stdout.log"), "stderr.log": sha(folder / "stderr.log")},
                  "resource_observations": {"complete": True}}
        (folder / "result.json").write_text(json.dumps(result))
        rows.append({"case": index, "stage": stage, "path": name, "expected": kind, "actual": kind,
                     "passed": True, "native_exit": result["exit_code"], "wrapper_exit": int(missing),
                     "expected_sha256": h, "resource_sha256": h,
                     "result_sha256": sha(folder / "result.json"),
                     "stdout_sha256": result["outputs"]["stdout.log"],
                     "stderr_sha256": result["outputs"]["stderr.log"],
                     "cleanup_scope": "provider_and_asset_finish"})
    (root / "matrix.json").write_text(json.dumps({"schema": "nashura.pure-linux-resource-matrix.v1",
                                                   "catalog_ready_pass": False, "cases": rows}))
    return sha(root / "matrix.json")


def test_verifier_reports_limited_scope(tmp_path):
    report = verify(tmp_path, build(tmp_path), "probe", "backend")
    assert report["cases"] == 13 and report["status"] == "PASS_LIMITED"
    assert report["catalog_ready_pass"] is False


@pytest.mark.parametrize("damage", ["matrix", "log", "resource", "probe"])
def test_verifier_rejects_changed_evidence(tmp_path, damage):
    matrix_hash = build(tmp_path)
    if damage == "matrix":
        (tmp_path / "matrix.json").write_text("{}")
    elif damage == "log":
        (tmp_path / "case-04/stdout.log").write_text("altered")
    elif damage == "resource":
        (tmp_path / "case-06/resource.bin").write_bytes(b"altered")
    with pytest.raises(ValueError):
        verify(tmp_path, matrix_hash, "different" if damage == "probe" else "probe", "backend")
