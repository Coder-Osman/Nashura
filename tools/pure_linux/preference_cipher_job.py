"""Observe three persistent cipher candidates in two isolated ARM64/Bionic processes."""

import argparse
import json
import os
from pathlib import Path
import platform

from native_init_job import check_inputs, digest, run


def validate_mappings(mapped, inputs):
    if not isinstance(mapped, list):
        return False
    crypto = [row for row in mapped if row["name"] == "libcrypto.so"]
    if len(crypto) != 1 or crypto[0]["sha256"] != inputs.get("libcrypto.so"):
        return False
    if any(row["name"] == "libg.so" for row in mapped):
        return False
    names = [row["name"] for row in mapped]
    if len(names) != len(set(names)) or not {"probe", "linker64", "libc.so", "libdl.so"}.issubset(names):
        return False
    for row in mapped:
        if row["name"] == "[vdso]":
            if row.get("path") != "[vdso]" or row.get("sha256") is not None:
                return False
        elif row.get("sha256") != inputs.get(row["name"]) or row["name"] not in inputs:
            return False
    return True


def validate(parsed, mapped, inputs):
    if not isinstance(parsed, dict) or not validate_mappings(mapped, inputs):
        return False
    if (parsed.get("status") != "PASS" or parsed.get("libg_loaded") is not False or
            parsed.get("scope") != "primitive_candidate_public_key_real_dex_iv" or
            parsed.get("known_answer_first_blocks") != 2 or parsed.get("boundary_cases") != 20):
        return False
    for name in ("nonaligned", "bad_padding"):
        sequence = parsed.get(name)
        if not isinstance(sequence, list) or len(sequence) != 4:
            return False
        if sequence[0] != {"result": -3, "length": 0, "matches": False, "output_unchanged": True}:
            return False
        for row in sequence:
            if set(row) != {"result", "length", "matches", "output_unchanged"}:
                return False
            if row["result"] not in {0, -3} or not isinstance(row["length"], int) or row["length"] < 0:
                return False
            if row["result"] and (row["length"] or row["output_unchanged"] is not True or row["matches"] is not False):
                return False
            if not row["result"] and (row["length"] not in {16, 32} or
                    row["matches"] is not (row["length"] == 16) or row["output_unchanged"] is not False):
                return False
    return True


def read_evidence(output):
    parsed, mapped, errors = None, [], []
    try:
        parsed = json.loads((output / "stdout.log").read_text())
    except (ValueError, UnicodeError, OSError) as error:
        errors.append("assertions:" + type(error).__name__)
    try:
        for line in (output / "stderr.log").read_text().splitlines():
            if line.startswith("unresolved_mapping:"):
                errors.append("unresolved_mapping")
            if not line.startswith("mapped:"):
                continue
            path = line.removeprefix("mapped:")
            if path == "[vdso]":
                mapped.append({"name": path, "path": path, "sha256": None})
            elif not path.startswith("/"):
                errors.append("nonabsolute_mapping")
            else:
                try:
                    mapped.append({"name": Path(path).name, "path": path, "sha256": digest(Path(path))})
                except OSError as error:
                    errors.append("mapping:" + type(error).__name__)
    except (UnicodeError, OSError) as error:
        errors.append("mapping_log:" + type(error).__name__)
    return parsed, mapped, errors


def main():
    parser = argparse.ArgumentParser()
    for name in ("payload", "probe", "iv", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("probe-sha256", "iv-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"arm64", "aarch64"}:
        raise ValueError("native ARM64 Linux required")
    before = check_inputs(args.payload)
    if digest(args.probe) != args.probe_sha256 or digest(args.iv) != args.iv_sha256 or args.iv.stat().st_size != 16:
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
                      str(args.payload / "libs" / "libcrypto.so"), str(args.iv)], output, environment, 20)
        parsed, mapped, errors = read_evidence(output)
        result.update(stdout_sha256=digest(output / "stdout.log"), stderr_sha256=digest(output / "stderr.log"),
                      assertion_summary=parsed, mapped_files=mapped, evidence_errors=errors,
                      passed=result["exit_code"] == 0 and not result["timed_out"] and not errors and
                      validate(parsed, mapped, {**before, "probe": args.probe_sha256}))
        records.append(result)
    after = check_inputs(args.payload)
    unchanged = before == after and digest(args.probe) == args.probe_sha256 and digest(args.iv) == args.iv_sha256
    repeat = records[0]["assertion_summary"] == records[1]["assertion_summary"]
    report = {"schema": "nashura.preference-cipher-native.v1", "machine": platform.machine(),
              "kernel": platform.release(), "records": records, "inputs_before": before,
              "inputs_after": after, "input_count": len(before), "inputs_unchanged": unchanged,
              "probe_sha256": args.probe_sha256, "iv_sha256": args.iv_sha256, "repeat_equal": repeat,
              "android_cipher_parity": "NOT_RUN", "application_key_identity": "UNRESOLVED",
              "storage_and_backup": "UNRESOLVED", "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return int(not unchanged or not repeat or not all(record["passed"] for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
