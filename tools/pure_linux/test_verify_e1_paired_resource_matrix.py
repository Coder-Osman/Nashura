import hashlib
import json

import pytest

import verify_e1_paired_resource_matrix as verifier
from run_e1_paired_resource_matrix import EXPECTED, read_stock


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root, monkeypatch):
    rows, stock = [], []
    for stage in {item[0] for item in EXPECTED}:
        folder = root / stage
        folder.mkdir()
        (folder / "asset.index").write_text("index")
        (folder / "resources.json").write_text("manifest")
    for index, (stage, name, kind) in enumerate(EXPECTED):
        missing = kind == "missing"
        content = None if missing else b"sample"
        operations = list(range(24))
        stock.append({"result": {"bytes": list(content) if content else None,
                                  "operation_status": -1 if missing else 0,
                                  "operations": operations}})
        folder = root / f"case-{index:02d}"
        folder.mkdir()
        lines = [f"PHASE\tresource_open_enter\t{name}\tTID\t1",
                 "RESOURCE_EXERCISE\t" + "\t".join(map(str, [-1 if missing else 0] + operations)),
                 "PROVIDER_SLOTS_RELEASED\t1", "PHASE\tafter_close", "NATIVE_ASSET_FINISH_RESULT\t0"]
        lines += (["RESOURCE_MISSING\t1", "RESOURCE_INVALID_FILE\t1", "PHASE\tresource_close_return"]
                  if missing else ["RESOURCE_FILE_KIND\t" + kind])
        (folder / "stdout.log").write_text("\n".join(lines))
        (folder / "stderr.log").write_text("")
        if content:
            (folder / "resource.bin").write_bytes(content)
        runtime = {"libg.so": verifier.ENGINE}
        result = {"inputs_before": runtime, "inputs_after": runtime, "inputs_unchanged": True,
                  "resources_before": {}, "resources_after": {}, "resources_unchanged": True,
                  "auxiliary_unchanged": True, "probe_unchanged": True, "postcheck_errors": [],
                  "timed_out": False, "probe_sha256": "probe", "backend_binding_verified": True,
                  "auxiliary_before": {"asset_backend": "backend", "asset_index": sha(root / stage / "asset.index"),
                                       "resource_manifest": sha(root / stage / "resources.json")},
                  "outputs": {name: sha(folder / name) for name in ("stdout.log", "stderr.log")},
                  "exit_code": 60 if missing else 0, "resource_observations": {"complete": True}}
        (folder / "result.json").write_text(json.dumps(result))
        rows.append({"case": index, "stage": stage, "path": name, "expected": kind, "actual": kind,
                     "paired_pass": True, "native_exit": result["exit_code"], "wrapper_exit": int(missing),
                     "resource_sha256": sha(folder / "resource.bin") if content else None,
                     "result_sha256": sha(folder / "result.json"),
                     "stdout_sha256": result["outputs"]["stdout.log"],
                     "stderr_sha256": result["outputs"]["stderr.log"]})
    matrix = {"schema": "nashura.pure-linux-e1-paired-resource-matrix.v1",
              "stock_sha256": verifier.ANDROID_STOCK_SHA, "catalog_ready_pass": False, "cases": rows}
    (root / "paired-matrix.json").write_text(json.dumps(matrix))
    monkeypatch.setattr(verifier, "read_stock", lambda _: stock)
    return sha(root / "paired-matrix.json")


def test_paired_verification_scope(tmp_path, monkeypatch):
    report = verifier.verify(tmp_path, tmp_path / "stock", build(tmp_path, monkeypatch), "probe", "backend")
    assert report["cases"] == 24 and report["status"] == "PASS_LIMITED"
    assert report["catalog_ready_pass"] is False and report["full_battle"] == "NOT_RUN"


@pytest.mark.parametrize("damage", ["matrix", "operation", "resource", "probe", "backend", "close"])
def test_paired_verification_rejects_changed_evidence(tmp_path, monkeypatch, damage):
    matrix_hash = build(tmp_path, monkeypatch)
    if damage == "matrix":
        (tmp_path / "paired-matrix.json").write_text("{}")
    elif damage == "resource":
        (tmp_path / "case-00/resource.bin").write_bytes(b"changed")
    elif damage in ("operation", "close"):
        index = 0 if damage == "operation" else 2
        folder = tmp_path / f"case-{index:02d}"
        path = folder / "stdout.log"
        content = path.read_text()
        path.write_text(content.replace("RESOURCE_EXERCISE\t0\t0", "RESOURCE_EXERCISE\t0\t99")
                        if damage == "operation" else content.replace("PHASE\tresource_close_return", ""))
        result_path = folder / "result.json"
        result = json.loads(result_path.read_text())
        result["outputs"]["stdout.log"] = sha(path)
        result_path.write_text(json.dumps(result))
        matrix_path = tmp_path / "paired-matrix.json"
        matrix = json.loads(matrix_path.read_text())
        matrix["cases"][index]["stdout_sha256"] = sha(path)
        matrix["cases"][index]["result_sha256"] = sha(result_path)
        matrix_path.write_text(json.dumps(matrix))
        matrix_hash = sha(matrix_path)
    with pytest.raises(ValueError):
        verifier.verify(tmp_path, tmp_path / "stock", matrix_hash,
                        "wrong" if damage == "probe" else "probe",
                        "wrong" if damage == "backend" else "backend")


def test_stock_identity_rejects_wrong_file(tmp_path):
    stock = tmp_path / "stock.json"
    stock.write_text("[]")
    with pytest.raises(ValueError, match="identity"):
        read_stock(stock)
