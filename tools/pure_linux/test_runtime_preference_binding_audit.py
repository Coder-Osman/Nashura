"""Synthetic tamper cases exercise the verifier, never engine parity."""

import hashlib
import json

import pytest

from runtime_preference_binding_audit import capture_identity, replay_audit
from test_preference_runtime_readonly import capture


def save(path, value):
    path.write_text(json.dumps(value))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def captured(tmp_path):
    root = tmp_path / "capture"
    root.mkdir()
    material = bytes(range(80))
    (root / "material.bin").write_bytes(material)
    files = {"apk": "fixture-apk", "libg": "fixture-libg"}
    for slot in range(2):
        path = root / f"original-{slot}.xml"
        path.write_text("<map />\n")
        files[f"xml{slot}"] = sha(path)
    process = {"pid": 71, "starttime": 1, "boot_id": "fixture"}
    result = {"status": "RUNTIME_OBSERVED_LIMITED", "material_sha256": sha(root / "material.bin"),
              "files_before": files, "files_after": files, "files_final": files,
              "process_before": process, "process_after": process, "process_final": process,
              "observations": [capture(material), capture(material)], "local_inputs_unchanged": True,
              "final_integrity_checks": {name: True for name in
                                         ("files_final", "process_final", "local_inputs_unchanged")}}
    save(root / "result.json", result)
    return root


@pytest.mark.parametrize("damage", ["none", "material", "files", "process", "checks", "repeat", "nonempty"])
def test_capture_requires_current_material_and_unchanged_empty_maps(captured, damage):
    result = json.loads((captured / "result.json").read_text())
    if damage == "material": (captured / "material.bin").write_bytes(bytes(80))
    if damage == "files": result["files_final"]["libg"] = "changed"
    if damage == "process": result["process_final"]["starttime"] = 2
    if damage == "checks": result["final_integrity_checks"]["files_final"] = False
    if damage == "repeat": result["observations"][1]["locale"] = "changed"
    if damage == "nonempty":
        path = captured / "original-0.xml"
        path.write_text('<map><string name="key">value</string></map>')
        for field in ("files_before", "files_after", "files_final"):
            result[field]["xml0"] = sha(path)
    save(captured / "result.json", result)
    if damage == "none":
        assert capture_identity(captured)["status"] == "RUNTIME_OBSERVED_LIMITED"
    else:
        with pytest.raises(ValueError): capture_identity(captured)


@pytest.fixture
def replayed(tmp_path, captured):
    root = tmp_path / "replay"
    transaction = tmp_path / "transaction"
    root.mkdir()
    transaction.mkdir()
    identity = capture_identity(captured)
    records = []
    for ordinal in range(2):
        directory = root / f"process-{ordinal}"
        directory.mkdir()
        for stage, android in (("target", "after_target_store"), ("restore", "after_restore_store")):
            for slot, index in ((0, 0), (1, 2)):
                content = '<map><string name="key">value</string></map>' if stage == "target" else "<map />\n"
                (directory / f"{stage}-{slot}.xml").write_text(content)
                (transaction / f"{android}-preference-{index}.xml").write_text(content)
                if stage == "restore": (directory / f"store-{slot}.xml").write_text(content)
        save(directory / "stdout.log", {"status": "LOCAL_REPLAY_COMPLETE", "target_result": -1,
                                        "restore_result": -1, "backup_requests": 2, "decode_exceptions": 0,
                                        "backup_service": "UNRESOLVED", "catalog_ready_pass": False,
                                        "libg_loaded": False})
        (directory / "stderr.log").write_text("")
        records.append({"exit_code": 0, "command": ["/linker64", "/probe", "/libcrypto.so", "/material.bin",
                                                    str(directory), "fixture-target"],
                        "outputs": {path.name: sha(path) for path in directory.iterdir()}})
    result = {"machine": "aarch64", "records": records,
              "inputs": {"material.bin": identity["material_sha256"],
                         **{f"original-{slot}.xml": identity["files_before"][f"xml{slot}"] for slot in range(2)}},
              "inputs_unchanged": True, "runtime_before": {name: "fixture" for name in
                                                          ("libcrypto.so", "libc.so", "libdl.so", "linker64")},
              "libg_loaded": False, "catalog_ready_pass": False, "probe_sha256": "fixture"}
    result["runtime_after"] = result["runtime_before"].copy()
    save(root / "result.json", result)
    return root, identity, transaction


@pytest.mark.parametrize("damage", ["none", "material", "exit", "runtime", "scope", "coverage", "target",
                                    "hash", "backup", "serialization", "map", "path"])
def test_replay_identity_coverage_bytes_and_failure_scope(replayed, damage):
    root, identity, transaction = replayed
    result = json.loads((root / "result.json").read_text())
    record = result["records"][0]
    if damage == "material": result["inputs"]["material.bin"] = "changed"
    if damage == "exit": record["exit_code"] = 1
    if damage == "runtime": result["runtime_after"]["libcrypto.so"] = "changed"
    if damage == "scope": result["catalog_ready_pass"] = True
    if damage == "coverage": record["outputs"].pop("target-0.xml")
    if damage == "target": record["command"][-1] = "other-target"
    if damage == "hash": (root / "process-0/target-0.xml").write_text("changed")
    if damage == "path": record["outputs"]["../bad"] = "unknown"
    if damage == "backup":
        path = root / "process-0/stdout.log"
        summary = json.loads(path.read_text())
        summary["backup_service"] = "PASS"
        save(path, summary)
        record["outputs"]["stdout.log"] = sha(path)
    if damage in ("serialization", "map"):
        path = transaction / "after_target_store-preference-0.xml"
        path.write_text('<map> <string name="key">%s</string></map>' %
                        ("value" if damage == "serialization" else "changed"))
    save(root / "result.json", result)
    if damage in ("none", "serialization", "map"):
        report = replay_audit(root, identity, transaction, "fixture-target")
        assert report["all_bytes_equal"] is (damage == "none")
        assert report["all_maps_equal"] is (damage != "map")
    else:
        with pytest.raises(ValueError): replay_audit(root, identity, transaction, "fixture-target")
