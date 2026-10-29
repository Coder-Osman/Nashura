"""Audit Android argument provenance and bounded Linux native factory execution."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_allocator_origin import compare as compare_allocator

ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def compare(android, linux, stdout):
    compare_allocator(android)
    coverage = {row["label"]: row["installed"] for row in android["trace"]["coverage"]}
    if coverage.get("owner_factory_argument_source_candidate") is not True:
        raise ValueError("argument source uncovered")
    factory = next(e for e in android["trace"]["events"] if e.get("kind") == "owner_holder_factory_return")
    source = factory.get("factory_argument_source") or {}
    entries = [e for e in android["trace"]["events"] if e.get("kind") == "owner_factory_argument_source_return"
               and e.get("id") == source.get("id")]
    if (len(entries) != 1 or source.get("caller") != "0x139dd10" or source.get("wrapper_nonnull") is not True or
            source.get("argument_nonnull") is not True or source.get("allocator_matches") is not True or
            factory.get("factory_argument_matches_parent_field") is not True or
            factory.get("factory_argument_table_module") != "libg.so" or
            factory.get("factory_argument_table_offset") != "0x18c8b40" or
            source.get("thread") != factory["allocator_constructor"]["thread"] or
            entries[0]["sequence"] >= factory["sequence"]):
        raise ValueError("Android argument provenance mismatch")
    if (linux.get("status") != "BLOCKED" or linux.get("exit_code") != 0 or linux.get("timed_out") is not False or
            linux.get("holder_factory_only") is not True or linux.get("inputs_before", {}).get("libg.so") != ENGINE or
            not all(linux.get(k) is True for k in ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged")) or
            linux.get("postcheck_errors") != [] or linux.get("catalog_ready_pass") is not False):
        raise ValueError("Linux identity or strict gate mismatch")
    lines = stdout.splitlines()
    expected = ["HOLDER_ARGUMENT_CREATED\t1", "HOLDER_FACTORY_CREATED\t1", "HOLDER_FACTORY_RELEASED\t1", "PROVIDER_RESULT\t0"]
    if any(lines.count(line) != 1 for line in expected) or [lines.index(x) for x in expected] != sorted(lines.index(x) for x in expected):
        raise ValueError("Linux holder lifecycle incomplete")
    return {"status": "NATIVE_VERIFIED_LIMITED", "engine_sha256": ENGINE,
            "android_argument_source_caller": source["caller"], "linux_argument_and_holder_created": True,
            "linux_native_cleanup_returned": True, "catalog_ready_pass": False,
            "limitations": ["isolated allocator and factory, not original parent lifetime", "no full trajectory comparison",
                            "holder setup and initialization not executed", "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", type=Path, required=True)
    parser.add_argument("--android-sha256", required=True)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--linux-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    if digest(args.android) != args.android_sha256:
        raise ValueError("Android identity mismatch")
    android = json.loads(args.android.read_text())
    if android.get("script_sha256") != digest(args.script):
        raise ValueError("script identity mismatch")
    linux_path = args.linux_dir / "result.json"
    linux = json.loads(linux_path.read_text())
    stdout = args.linux_dir / "stdout.log"
    if digest(stdout) != linux.get("outputs", {}).get("stdout.log"):
        raise ValueError("Linux stdout identity mismatch")
    result = compare(android, linux, stdout.read_text())
    result.update(android_sha256=args.android_sha256, linux_sha256=digest(linux_path), script_sha256=digest(args.script))
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
