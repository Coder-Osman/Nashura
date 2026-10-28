"""Bind a recorded Android transaction to isolated native runs; keep catalog closed."""

import argparse
import hashlib
import json
from pathlib import Path

from loading_store_audit import APP, audit as store_audit, calls_from, require
from native_backup_binding_audit import audit as native_audit
from preference_backend_job import entries
from run_preference_runtime_readonly import validate_capture


def digest(path):
    require(path.is_file() and not path.is_symlink(), "regular evidence file required")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    digest(path)
    return json.loads(path.read_text())


def capture_identity(root):
    result = read(root / "result.json")
    material = (root / "material.bin").read_bytes()
    require(digest(root / "material.bin") == result["material_sha256"], "material identity")
    require(result["status"] == "RUNTIME_OBSERVED_LIMITED", "capture status")
    require(result["files_before"] == result["files_after"] == result["files_final"], "capture files")
    require(result["process_before"] == result["process_after"] == result["process_final"], "capture process")
    require(result["local_inputs_unchanged"] is True and
            set(result["final_integrity_checks"]) == {"files_final", "process_final", "local_inputs_unchanged"} and
            all(value is True for value in result["final_integrity_checks"].values()), "capture integrity")
    observations = result["observations"]
    require(len(observations) == 2 and observations[0] == observations[1], "repeat capture")
    for observation in observations:
        validate_capture(observation, result["process_before"]["pid"], material)
    for slot in range(2):
        path = root / f"original-{slot}.xml"
        require(digest(path) == result["files_before"][f"xml{slot}"], "initial XML identity")
        require(entries(path) == {}, "this audit covers observed empty initial maps only")
    return result


def replay_audit(root, capture, transaction, target):
    result = read(root / "result.json")
    require(result["machine"] == "aarch64" and len(result["records"]) == 2, "replay shape")
    expected = {"material.bin": capture["material_sha256"],
                **{f"original-{slot}.xml": capture["files_before"][f"xml{slot}"] for slot in range(2)}}
    require(result["inputs"] == expected and result["inputs_unchanged"] is True, "replay inputs")
    require(result["runtime_before"] == result["runtime_after"] and
            {"libcrypto.so", "libc.so", "libdl.so", "linker64"} == set(result["runtime_before"]), "replay runtime")
    require(result["libg_loaded"] is False and result["catalog_ready_pass"] is False, "replay scope")
    comparisons = []
    for ordinal, record in enumerate(result["records"]):
        require(record["exit_code"] == 0, "replay exit")
        directory = root / f"process-{ordinal}"
        required = {"stdout.log", "stderr.log", "store-0.xml", "store-1.xml",
                    "target-0.xml", "target-1.xml", "restore-0.xml", "restore-1.xml"}
        require(required.issubset(record["outputs"]), "replay output coverage")
        for name, sha in record["outputs"].items():
            require(Path(name).name == name and digest(directory / name) == sha, "replay output identity")
        command = record["command"]
        require(len(command) == 6 and Path(command[1]).name == "probe" and
                Path(command[3]).name == "material.bin" and command[-1] == target, "replay transaction input")
        summary = read(directory / "stdout.log")
        require(summary["status"] == "LOCAL_REPLAY_COMPLETE" and
                summary["target_result"] == summary["restore_result"] == -1 and
                summary["backup_requests"] == 2 and summary["decode_exceptions"] == 0 and
                summary["backup_service"] == "UNRESOLVED" and summary["catalog_ready_pass"] is False and
                summary["libg_loaded"] is False, "replay summary")
        for stage, android_stage in (("target", "after_target_store"), ("restore", "after_restore_store")):
            for slot, index in ((0, 0), (1, 2)):
                native = directory / f"{stage}-{slot}.xml"
                android = transaction / f"{android_stage}-preference-{index}.xml"
                comparisons.append({"ordinal": ordinal, "stage": stage, "slot": slot,
                                    "native_sha256": digest(native), "android_sha256": digest(android),
                                    "byte_equal": native.read_bytes() == android.read_bytes(),
                                    "map_equal": entries(native) == entries(android)})
                if stage == "restore":
                    require(native.read_bytes() == (directory / f"store-{slot}.xml").read_bytes(), "restore persistence")
    return {"result_sha256": digest(root / "result.json"), "probe_sha256": result["probe_sha256"],
            "runtime": result["runtime_before"], "native_exits": [0, 0], "comparisons": comparisons,
            "all_bytes_equal": all(row["byte_equal"] for row in comparisons),
            "all_maps_equal": all(row["map_equal"] for row in comparisons)}


def audit(capture_root, transaction, native_root, replay_root):
    capture = capture_identity(capture_root)
    controller = read(transaction / "controller.json")
    require(controller["exit_code"] == 0 and controller["unchanged"] is True and
            controller["before"] == controller["after"], "transaction integrity")
    require(controller["before"] == {"files": capture["files_before"],
                                     "process": capture["process_before"]}, "transaction runtime binding")
    store = store_audit(transaction / "trace.jsonl", controller["trace_sha256"])
    rows = [json.loads(line) for line in (transaction / "trace.jsonl").read_text().splitlines()]
    calls, _, _ = calls_from(rows)
    target = next(call["args"][1] for call in calls if call["method"] == APP + ".storeKeyValue")
    native = native_audit(native_root, expected_entries=(1, 1))
    for ordinal in range(3):
        require(digest(native_root / "material.bin") == capture["material_sha256"], "native material copy")
        result = read(native_root / f"process-{ordinal}/result.json")
        expected = {"preference_material": capture["material_sha256"],
                    **{f"preference_xml{slot}": capture["files_before"][f"xml{slot}"] for slot in range(2)}}
        require(result["auxiliary_before"] == result["auxiliary_after"], "native auxiliary identity")
        for key, sha in expected.items():
            require(result["auxiliary_before"][key] == sha, "native explicit preference identity")
        require(result["inputs_before"]["libg.so"] == capture["files_before"]["libg"], "native libg identity")
        wrapper = read(native_root / "batch-result.json")["records"][ordinal]["command"]
        for option, name in (("preference-material", "material.bin"),
                             ("preference-xml0", "original-0.xml"), ("preference-xml1", "original-1.xml")):
            require(wrapper.count("--" + option) == 1, "native option multiplicity")
            require(Path(wrapper[wrapper.index("--" + option) + 1]).name == name, "native preference argument")
            require(digest(native_root / name) == expected[option.replace("-", "_")], "native file binding")
    replay = replay_audit(replay_root, capture, transaction, target)
    require(replay["all_bytes_equal"] and replay["all_maps_equal"], "native/Android replay mismatch")
    return {"schema": "nashura.runtime-preference-binding-audit.v1", "audit": "PASS",
            "scope": "current runtime identity and one ASCII empty-map transaction only",
            "capture_sha256": digest(capture_root / "result.json"),
            "controller_sha256": digest(transaction / "controller.json"),
            "current_runtime_identity": "RUNTIME_OBSERVED_BOUND", "historical_matches": capture["historical_matches"],
            "input_identity": {"apk_sha256": capture["files_before"]["apk"],
                               "libg_sha256": capture["files_before"]["libg"],
                               "dex_sha256": capture["dex_sha256"]},
            "android_transaction": store, "native_batch": native, "replay": replay,
            "android_backup_parity": "NOT_VERIFIED", "configuration_contract_pass": False,
            "catalog_ready_pass": False, "resource_manager": "NOT_RUN", "gamestate": "NOT_RUN"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "transaction", "native", "replay", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.capture, args.transaction, args.native, args.replay)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
