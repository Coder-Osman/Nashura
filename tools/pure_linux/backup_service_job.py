"""Bound a real Binder backup-service lookup without starting Android services."""

import argparse
import json
import os
from pathlib import Path
import platform

from native_init_job import check_inputs, digest, run


def classify(exit_code, timed_out, stdout):
    phases = [line.split("\t", 1)[1] for line in stdout.splitlines()
              if line.startswith("BACKUP_PHASE\t")]
    discovery = [line.split("\t", 1)[1] for line in stdout.splitlines()
                 if line.startswith("BACKUP_DISCOVERY\t")]
    expected = ["before_binder_load", "after_binder_load",
                "before_check_service", "after_check_service"]
    observed = "UNRESOLVED"
    if not timed_out and phases == expected:
        if exit_code == 93 and discovery == ["service_absent_not_contract_success"]:
            observed = "SERVICE_ABSENT"
        elif exit_code == 94 and discovery == ["service_found_contract_not_exercised"]:
            observed = "SERVICE_FOUND"
    return {"phases": phases, "lookup_result": observed,
            "backup_contract": "UNRESOLVED", "status": "BLOCKED",
            "catalog_ready_pass": False, "libg_lifecycle_exercised": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("payload", "probe", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--probe-sha256", required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("ARM64 Linux required")
    before = check_inputs(args.payload)
    if digest(args.probe) != args.probe_sha256:
        raise ValueError("probe identity differs")
    if not {"libbinder_ndk.so", "libbinder.so"}.issubset(before):
        raise ValueError("matching Binder inputs required")
    args.output.mkdir(exist_ok=False)
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("LD_", "NASHURA_"))}
    environment["LD_LIBRARY_PATH"] = str(args.payload / "libs")
    records = []
    for index in range(2):
        output = args.output / ("process-" + str(index))
        output.mkdir()
        command = ["strace", "-ff", "-tt", "-s", "128", "-o", str(output / "syscalls"),
                   "-e", "trace=openat,close,ioctl,exit_group",
                   str(args.payload / "linker64"), str(args.probe),
                   str(args.payload / "libs" / "libbinder_ndk.so")]
        record = run(command, output, environment, 10)
        record.update(classify(record["exit_code"], record["timed_out"],
                               (output / "stdout.log").read_text(errors="replace")))
        record["outputs"] = {path.name: digest(path) for path in sorted(output.iterdir())
                             if path.is_file()}
        records.append(record)
    after = check_inputs(args.payload)
    report = {"schema": "nashura.backup-service-discovery.v1",
              "machine": platform.machine(), "kernel": platform.release(),
              "inputs_before": before, "inputs_after": after,
              "probe_sha256": args.probe_sha256,
              "inputs_unchanged": before == after and digest(args.probe) == args.probe_sha256,
              "records": records, "status": "BLOCKED",
              "backup_contract": "UNRESOLVED", "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
