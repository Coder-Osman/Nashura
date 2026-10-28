"""Audit a version-bound holder factory and initialization association."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_owner_init import compare as compare_init


def compare(data):
    compare_init(data)
    trace = data["trace"]
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if not all(coverage.get(name) is True for name in ("owner_holder_factory_candidate", "owner_holder_setup_candidate")):
        raise ValueError("holder hooks missing")
    kinds = ("owner_holder_factory_return", "owner_holder_setup_enter", "owner_holder_setup_return", "engine_owner_init_enter")
    groups = [[event for event in trace["events"] if event.get("kind") == kind] for kind in kinds]
    if any(len(group) != 1 for group in groups):
        raise ValueError("holder observations incomplete")
    factory, setup, returned, init = [group[0] for group in groups]
    if (factory.get("holder_nonnull") is not True or factory.get("allocator_nonnull") is not True or
            factory.get("allocator_matches_holder") is not True or factory.get("allocator_vtable_module") != "libg.so" or
            factory.get("allocator_vtable_offset") != "0x18bf798" or setup.get("caller") != "0x139e330" or
            setup.get("factory_id") != factory.get("id") or setup.get("slot_before_nonnull") is not False or
            returned.get("id") != setup.get("id") or returned.get("slot_after_nonnull") is not True or
            init.get("holder_setup_id") != setup.get("id") or
            any(event.get("thread") != factory.get("thread") for event in (setup, returned, init)) or
            not factory["sequence"] < setup["sequence"] < returned["sequence"] < init["sequence"]):
        raise ValueError("holder factory association invalid")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "allocator_vtable_offset": "0x18bf798",
            "setup_caller": "0x139e330", "slot_before_nonnull": False, "slot_after_nonnull": True,
            "owner_init_argument_matches_setup": True, "catalog_ready_pass": False,
            "limitations": ["allocator lifetime and Linux initialization unresolved", "not a complete trajectory comparison"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--result-sha256", required=True)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.result.read_bytes()
    result_sha = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    script_sha = hashlib.sha256(args.script.read_bytes()).hexdigest()
    if result_sha != args.result_sha256 or data.get("script_sha256") != script_sha:
        raise ValueError("input identity mismatch")
    summary = compare(data)
    summary.update(result_sha256=result_sha, script_sha256=script_sha)
    with args.output.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
