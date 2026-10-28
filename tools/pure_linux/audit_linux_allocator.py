"""Audit the bounded true-libg allocator experiment."""

import argparse
import hashlib
import json
from pathlib import Path

ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def compare(result, stdout):
    if (result.get("status") != "BLOCKED" or result.get("exit_code") != 0 or
            result.get("timed_out") is not False or result.get("allocator_only") is not True or
            result.get("inputs_before", {}).get("libg.so") != ENGINE or
            not all(result.get(k) is True for k in
                    ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged")) or
            result.get("postcheck_errors") != [] or result.get("catalog_ready_pass") is not False):
        raise ValueError("identity, exit or gate mismatch")
    lines = stdout.splitlines()
    expected = ["ALLOCATOR_CONSTRUCTOR\t1", "ALLOCATOR_ALLOCATION\t1", "ALLOCATOR_RELEASE\t1",
                "ALLOCATOR_INVALID_REJECTED\t1", "PROVIDER_RESULT\t0"]
    if any(lines.count(line) != 1 for line in expected) or [lines.index(x) for x in expected] != sorted(lines.index(x) for x in expected):
        raise ValueError("allocator lifecycle incomplete")
    return {"status": "NATIVE_VERIFIED_LIMITED", "engine_sha256": ENGINE, "native_exit_code": 0,
            "constructor": "OBSERVED", "allocate_and_write_64": "OBSERVED", "release": "OBSERVED",
            "invalid_alignment_and_size_rejected": True, "catalog_ready_pass": False,
            "limitations": ["one isolated allocator instance", "no Android allocation parity or concurrent stress",
                            "factory and allocator parent lifetime unverified", "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--result-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = (args.directory / "result.json").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.result_sha256:
        raise ValueError("result identity mismatch")
    result = json.loads(raw)
    output = (args.directory / "stdout.log").read_bytes()
    if hashlib.sha256(output).hexdigest() != result.get("outputs", {}).get("stdout.log"):
        raise ValueError("stdout identity mismatch")
    summary = compare(result, output.decode())
    summary["result_sha256"] = digest
    with args.output.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
