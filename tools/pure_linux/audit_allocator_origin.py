"""Audit the observed allocator constructor and cross-thread factory linkage."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_holder_factory import compare as compare_factory


def compare(data):
    compare_factory(data)
    trace = data["trace"]
    if not any(row.get("label") == "owner_allocator_constructor_candidate" and row.get("installed") is True
               for row in trace["coverage"]):
        raise ValueError("allocator constructor hook missing")
    factory = next(e for e in trace["events"] if e.get("kind") == "owner_holder_factory_return")
    ctor = factory.get("allocator_constructor") or {}
    matching = [e for e in trace["events"] if e.get("kind") == "owner_allocator_constructor_return" and
                e.get("id") == ctor.get("id")]
    if (len(matching) != 1 or ctor.get("caller") != "0x139dd04" or
            ctor.get("argument_text") != "AndroidMainAllocator" or ctor.get("argument_nonnull") is not True or
            ctor.get("field_matches_argument") is not True or ctor.get("vtable_matches") is not True or
            factory.get("factory_argument_nonnull") is not True or factory.get("factory_argument_matches_field") is not True or
            matching[0].get("thread") != ctor.get("thread") or matching[0]["sequence"] >= factory["sequence"]):
        raise ValueError("allocator construction linkage invalid")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "constructor_caller": ctor["caller"],
            "constructor_argument": ctor["argument_text"],
            "constructor_and_factory_same_thread": ctor["thread"] == factory["thread"],
            "factory_second_argument_nonnull": True, "catalog_ready_pass": False,
            "limitations": ["address linkage does not prove complete lifetime", "factory second argument type unresolved",
                            "Linux allocator construction not attempted"]}


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
