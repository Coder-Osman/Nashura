"""Observe the file primitive in two isolated ARM64/Bionic process trees."""

import argparse
import json
import os
from pathlib import Path
import platform

from native_init_job import check_inputs, digest, run
from preference_cipher_job import read_evidence


CASES = ("two_slots_and_exec_restart", *("failure-" + str(index) for index in range(6)),
         *("crash-before-" + str(index) for index in range(1, 6)), "crash_after_commit",
         "restore_failure_preserves_backup", "existing_backup_wins_after_repeated_write",
         "capacity_and_output_guard", "lock_and_owner",
         "nonregular_and_oversize_rejected", "directory_and_path_rejected")


def validate(parsed, mapped, identities):
    if not isinstance(parsed, dict) or set(parsed) != {
            "status", "scope", "libg_loaded", "cases", "case_count", "assertions"}:
        return False
    if (parsed["status"] != "PASS" or parsed["scope"] != "synthetic_file_commit_and_process_crash_only" or
            parsed["libg_loaded"] is not False or parsed["cases"] != list(CASES) or
            type(parsed["case_count"]) is not int or parsed["case_count"] != len(CASES) or
            type(parsed["assertions"]) is not int or parsed["assertions"] < 300):
        return False
    if not isinstance(mapped, list) or any(not isinstance(row, dict) for row in mapped):
        return False
    names = [row.get("name") for row in mapped]
    if (len(names) != len(set(names)) or not {"probe", "linker64", "libc.so", "libdl.so"}.issubset(names) or
            {"libg.so", "libcrypto.so", "libart.so", "libandroid.so"}.intersection(names)):
        return False
    for row in mapped:
        if row.get("name") == "[vdso]":
            if row.get("path") != "[vdso]" or row.get("sha256") is not None:
                return False
        elif (row.get("name") not in identities or row.get("sha256") != identities[row["name"]] or
              not isinstance(row.get("path"), str) or not row["path"].startswith("/")):
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("payload", "probe", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--probe-sha256", required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("native ARM64 Linux required")
    before = check_inputs(args.payload)
    if digest(args.probe) != args.probe_sha256:
        raise ValueError("probe identity mismatch")
    args.output.mkdir(exist_ok=False)
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("LD_", "NASHURA_"))}
    environment["LD_LIBRARY_PATH"] = str(args.payload / "libs")
    records = []
    for index in range(2):
        output = args.output / ("process-" + str(index))
        output.mkdir()
        result = run([str(args.payload / "linker64"), str(args.probe), str(output / "cases"),
                      str(args.payload / "linker64")], output, environment, 30)
        parsed, mapped, errors = read_evidence(output)
        result.update(assertion_summary=parsed, mapped_files=mapped, evidence_errors=errors,
                      stdout_sha256=digest(output / "stdout.log"), stderr_sha256=digest(output / "stderr.log"),
                      passed=result["exit_code"] == 0 and not result["timed_out"] and not errors and
                      validate(parsed, mapped, {**before, "probe": args.probe_sha256}))
        records.append(result)
    after = check_inputs(args.payload)
    unchanged = before == after and digest(args.probe) == args.probe_sha256
    repeat = records[0]["assertion_summary"] == records[1]["assertion_summary"]
    report = {"schema": "nashura.preference-files-native.v1", "machine": platform.machine(),
              "kernel": platform.release(), "inputs_before": before, "inputs_after": after,
              "input_count": len(before), "inputs_unchanged": unchanged, "repeat_equal": repeat,
              "probe_sha256": args.probe_sha256, "records": records,
              "android_file_parity": "NOT_RUN", "xml_and_map_semantics": "NOT_RUN",
              "current_runtime_identity": "UNRESOLVED", "backup_service": "UNRESOLVED",
              "libg_bridge_integration": "NOT_RUN", "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return int(not unchanged or not repeat or not all(row["passed"] for row in records))


if __name__ == "__main__":
    raise SystemExit(main())
