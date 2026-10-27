"""Check private reference capsules in two identity-bound ARM64/Bionic children."""

import argparse
import json
import os
from pathlib import Path
import platform

from native_init_job import check_inputs, digest, run
from preference_cipher_job import read_evidence, validate_mappings


def validate(parsed, mapped, identities, expected_records):
    return (isinstance(parsed, dict) and parsed == {
        "status": "PASS", "records": expected_records, "libg_loaded": False,
        "scope": "explicit_private_reference_bytes_only"} and
        type(parsed["records"]) is int and 1 <= expected_records <= 64 and
        validate_mappings(mapped, identities))


def main():
    parser = argparse.ArgumentParser()
    for name in ("payload", "probe", "capsule", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("probe-sha256", "capsule-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expected-records", required=True, type=int)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("native ARM64 Linux required")
    if not 1 <= args.expected_records <= 64:
        raise ValueError("bounded reference count required")
    before = check_inputs(args.payload)
    if digest(args.probe) != args.probe_sha256 or digest(args.capsule) != args.capsule_sha256:
        raise ValueError("experiment identity mismatch")
    args.output.mkdir(exist_ok=False)
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("LD_", "NASHURA_"))}
    environment["LD_LIBRARY_PATH"] = str(args.payload / "libs")
    records = []
    for index in range(2):
        output = args.output / ("process-" + str(index))
        output.mkdir()
        result = run([str(args.payload / "linker64"), str(args.probe),
                      str(args.payload / "libs/libcrypto.so"), str(args.capsule)], output, environment, 20)
        parsed, mapped, errors = read_evidence(output)
        result.update(assertion_summary=parsed, mapped_files=mapped, evidence_errors=errors,
                      stdout_sha256=digest(output / "stdout.log"), stderr_sha256=digest(output / "stderr.log"),
                      passed=result["exit_code"] == 0 and not result["timed_out"] and not errors and
                      validate(parsed, mapped, {**before, "probe": args.probe_sha256}, args.expected_records))
        records.append(result)
    after = check_inputs(args.payload)
    unchanged = (before == after and digest(args.probe) == args.probe_sha256 and
                 digest(args.capsule) == args.capsule_sha256)
    repeat = records[0]["assertion_summary"] == records[1]["assertion_summary"]
    report = {"schema": "nashura.preference-reference-native.v1", "machine": platform.machine(),
              "kernel": platform.release(), "inputs_before": before, "inputs_after": after,
              "input_count": len(before), "inputs_unchanged": unchanged, "repeat_equal": repeat,
              "probe_sha256": args.probe_sha256, "records": records,
              "current_runtime_binding": "UNRESOLVED", "storage_and_backup": "UNRESOLVED",
              "android_full_cipher_parity": "NOT_RUN", "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return int(not unchanged or not repeat or not all(row["passed"] for row in records))


if __name__ == "__main__":
    raise SystemExit(main())
