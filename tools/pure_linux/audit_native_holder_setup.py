"""Audit bounded Android/Linux holder setup observations."""

import argparse
import hashlib
import json
from pathlib import Path


ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
LIFECYCLE_LINES = (
    "HOLDER_SETUP_INPUT_READY\t1", "HOLDER_SETUP_SLOT_BEFORE\t0", "HOLDER_SETUP_NATIVE_RETURN\t1",
    "HOLDER_SETUP_SLOT_AFTER\t1", "HOLDER_SETUP_ALLOCATION_RANGE\t1",
    "HOLDER_SETUP_PAYLOAD_RANGE\t1", "HOLDER_SETUP_ALLOCATOR_MATCH\t1",
    "HOLDER_SETUP_ALLOCATION_SELF_MATCH\t1", "HOLDER_SETUP_OBJECT_OFFSET\t24",
    "HOLDER_SETUP_RETURNED\t1", "HOLDER_SETUP_FILESYSTEM_PRESENT\t1",
    "HOLDER_SETUP_FILESYSTEM_REPLACED\t1", "HOLDER_SETUP_SLOT_CLEARED\t1",
    "HOLDER_SETUP_FILESYSTEM_RETAINED\t1", "HOLDER_SETUP_RELEASED\t1", "PROVIDER_RESULT\t0",
)


def inspect_lifecycle(stdout, stderr=""):
    lines = stdout.splitlines()
    if any(lines.count(line) != 1 for line in LIFECYCLE_LINES):
        raise ValueError("Linux setup lifecycle incomplete")
    order = [lines.index(line) for line in LIFECYCLE_LINES]
    if order != sorted(order):
        raise ValueError("Linux setup lifecycle out of order")
    begin, end = lines.index("HOLDER_SETUP_FILESYSTEM_PRESENT\t1"), lines.index("HOLDER_SETUP_SLOT_CLEARED\t1")
    paths = {}
    for line in lines[begin:end]:
        fields = line.split("\t", 4)
        if len(fields) == 5 and fields[0] == "PROVIDER_STRING" and fields[2] == "8":
            name, length, path = fields[1], fields[3], fields[4]
            if name in paths or name not in ("data", "update") or int(length) != len(path.encode()):
                raise ValueError("invalid setup provider observation")
            paths[name] = path
    if set(paths) != {"data", "update"}:
        raise ValueError("missing setup provider observations")
    return {"linux_setup_object_verified": True, "linux_native_cleanup_returned": True,
            "linux_payload_offset": 24, "setup_provider_path_lengths": {k: len(v.encode()) for k, v in paths.items()},
            "java_exception_reported": "java.lang." in stderr,
            "holder_setup_semantics_pass": False, "catalog_ready_pass": False}


def compare(android, linux, stdout, stderr=""):
    trace = android.get("trace", {})
    if trace.get("errors") != [] or trace.get("lost") != 0:
        raise ValueError("Android trace lost setup evidence")
    coverage = {row.get("label"): row.get("installed") for row in trace.get("coverage", [])}
    if coverage.get("owner_holder_setup_candidate") is not True:
        raise ValueError("setup entry was not covered")
    events = trace.get("events", [])
    enter = next((row for row in events if row.get("kind") == "owner_holder_setup_enter"), None)
    returned = next((row for row in events if row.get("kind") == "owner_holder_setup_return"), None)
    init = next((row for row in events if row.get("kind") == "engine_owner_init_enter"), None)
    if not enter or not returned or not init:
        raise ValueError("setup lifecycle events incomplete")
    if (enter.get("caller") != "0x139e330" or enter.get("slot_before_nonnull") is not False or
            returned.get("id") != enter.get("id") or returned.get("slot_after_nonnull") is not True or
            init.get("holder_setup_id") != enter.get("id") or
            init.get("argument_matches_getter_holder_field") is not True or
            not (enter.get("sequence", 0) < returned.get("sequence", 0) < init.get("sequence", 0))):
        raise ValueError("Android setup association mismatch")
    # run_android_catalog_start_trace.py writes inventory identity at the result
    # root; retain the nested fallback for older captured fixtures.
    before = android.get("update_before", trace.get("update_before"))
    after = android.get("update_after", trace.get("update_after"))
    if not before or before != after:
        raise ValueError("Android update inventory changed")
    if (linux.get("status") != "HOLDER_SETUP_OBSERVED" or linux.get("exit_code") != 0 or
            linux.get("timed_out") is not False or linux.get("holder_setup_only") is not True or
            linux.get("inputs_before", {}).get("libg.so") != ENGINE or
            linux.get("catalog_ready_pass") is not False or
            not all(linux.get(key) is True for key in
                    ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged", "probe_unchanged")) or
            linux.get("postcheck_errors") != []):
        raise ValueError("Linux setup identity or gate mismatch")
    observed = inspect_lifecycle(stdout, stderr)
    return {
        "status": "NATIVE_VERIFIED_LIMITED",
        "engine_sha256": ENGINE,
        "android_setup_caller": enter["caller"],
        "android_slot_transition": {"before": False, "after": True},
        **observed,
        "limitations": [
            "isolated holder setup and cleanup, not original parent lifetime",
            "setup side effects are observed only through bounded output and identity checks",
            "native return and no pending JNI exception do not prove Java callbacks succeeded",
            "Android prefix layout and original parent lifetime have not been compared",
            "no ResourceManager, GameState, Step, or full trajectory comparison",
        ],
    }


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
    stdout = args.linux_dir / "stdout.log"
    stderr = args.linux_dir / "stderr.log"
    linux = json.loads(linux_path.read_text())
    for path in (stdout, stderr):
        if digest(path) != linux.get("outputs", {}).get(path.name):
            raise ValueError("Linux output identity mismatch")
    result = compare(android, linux, stdout.read_text(errors="replace"), stderr.read_text(errors="replace"))
    result.update(android_sha256=args.android_sha256, linux_sha256=digest(linux_path), script_sha256=digest(args.script))
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
