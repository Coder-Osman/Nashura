"""Validate a bounded Android owner-construction observation."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_database_factory import compare_factory
from audit_database_owner import compare_owner

ENGINE_SHA = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def compare(data):
    trace = data["trace"]
    if (data.get("status") != "RUNTIME_OBSERVED_REQUIRES_AUDIT" or
            data.get("engine_sha256") != ENGINE_SHA or data.get("script_error_count") != 0 or
            data.get("update_unchanged") is not True or data.get("update_before") != data.get("update_after") or
            trace.get("errors") != [] or trace.get("lost") != 0):
        raise ValueError("trace or identity incomplete")
    coverage = {row["label"]: row["installed"] for row in trace["coverage"]}
    if not all(coverage.get(label) is True for label in
               ("engine_owner_getter_candidate", "engine_owner_constructor_candidate", "engine_db_init_candidate")):
        raise ValueError("owner hook coverage incomplete")
    compare_owner(data)
    compare_factory(data)
    entries = [event for event in trace["events"] if event.get("kind") == "engine_db_init_enter"]
    if len(entries) != 1:
        raise ValueError("owner initialization count mismatch")
    entry = entries[0]
    ctor = entry.get("owner_constructor") or {}
    getter = entry.get("owner_getter") or {}
    parent = ctor.get("getter_parent") or {}
    if (entry.get("owner_vtable") != "0x17ea568" or ctor.get("caller") != "0x755804" or
            ctor.get("returned") is not True or ctor.get("vtable_matches") is not True or
            ctor.get("prior_address_observations") != 0 or getter != parent or
            type(getter.get("id")) is not int or getter.get("caller") != "0x13a43e0" or
            getter.get("thread") != entry.get("thread") or ctor.get("thread") != entry.get("thread")):
        raise ValueError("owner constructor linkage incomplete")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "engine_sha256": ENGINE_SHA,
            "update_inventory": data["update_before"], "getter_caller": getter["caller"],
            "constructor_caller": ctor["caller"], "owner_vtable": entry["owner_vtable"],
            "same_thread": True, "constructor_returned": True, "catalog_ready_pass": False,
            "limitations": ["address correlation does not establish complete lifetime",
                            "Linux constructor window not observed", "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--result-sha256", required=True)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.result.read_bytes()
    result_sha = hashlib.sha256(raw).hexdigest()
    if result_sha != args.result_sha256:
        raise ValueError("result identity mismatch")
    data = json.loads(raw)
    script_sha = hashlib.sha256(args.script.read_bytes()).hexdigest()
    if data.get("script_sha256") != script_sha:
        raise ValueError("script identity mismatch")
    summary = compare(data)
    summary.update(result_sha256=result_sha, script_sha256=script_sha)
    with args.output.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
