"""Run the bounded configuration candidate in ARM64/Bionic; no game lifecycle."""

import argparse
import json
import os
from pathlib import Path
import platform

from native_init_job import check_inputs, digest, run


def main():
    parser = argparse.ArgumentParser()
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
        result = run([str(args.payload / "linker64"), str(args.probe)], output, environment, 20)
        try:
            parsed = json.loads((output / "stdout.log").read_text())
        except (ValueError, UnicodeError):
            parsed = None
        expected = {"status": "PASS", "scope": "synthetic_backend_only", "failure_boundaries": 15,
                    "scenario_groups": 8, "libg_loaded": False}
        result.update(stdout_sha256=digest(output / "stdout.log"), stderr_sha256=digest(output / "stderr.log"),
                      assertion_summary=parsed,
                      passed=result["exit_code"] == 0 and not result["timed_out"] and parsed == expected)
        records.append(result)
    after = check_inputs(args.payload)
    probe_unchanged = digest(args.probe) == args.probe_sha256
    result = {"schema": "nashura.preference-contract-native.v1", "machine": platform.machine(),
              "kernel": platform.release(), "records": records,
              "inputs_before": before, "inputs_after": after, "inputs_unchanged": before == after,
              "probe_sha256": args.probe_sha256, "probe_unchanged": probe_unchanged,
              "scope": "synthetic backend with C semantic candidate under Bionic; no libg loaded",
              "android_runtime_parity": "NOT_RUN", "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return int(not all(record["passed"] for record in records) or before != after or not probe_unchanged)


if __name__ == "__main__":
    raise SystemExit(main())
