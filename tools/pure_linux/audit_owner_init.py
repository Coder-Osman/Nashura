"""Audit the Android owner initialization scope without exposing pointers."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_owner_constructor import compare as compare_owner


def compare(data):
    compare_owner(data)
    trace = data["trace"]
    if not any(row.get("label") == "engine_owner_init_candidate" and row.get("installed") is True
               for row in trace["coverage"]):
        raise ValueError("owner initialization coverage missing")
    kinds = ("engine_owner_init_enter", "engine_db_init_enter", "engine_db_init_return", "engine_owner_init_return")
    groups = [[event for event in trace["events"] if event.get("kind") == kind] for kind in kinds]
    if any(len(group) != 1 for group in groups):
        raise ValueError("initialization sequence incomplete")
    entry, db, db_return, returned = [group[0] for group in groups]
    if (entry.get("caller") != "0x13a1c88" or entry.get("owner_constructor_id") != db["owner_constructor"]["id"] or
            entry.get("argument_nonnull") is not True or entry.get("argument_readable") is not True or
            entry.get("argument_first_word_mapped") is not False or entry.get("argument_first_word_module") is not None or
            entry.get("argument_matches_slot_before") is not True or
            entry.get("argument_matches_getter_holder_field") is not True or
            returned.get("argument_matches_slot_after") is not True or returned.get("id") != entry.get("id") or
            db.get("owner_init_id") != entry.get("id") or db_return.get("id") != db.get("id") or
            any(event.get("thread") != entry.get("thread") for event in (db, db_return, returned)) or
            not entry["sequence"] < db["sequence"] < db_return["sequence"] < returned["sequence"]):
        raise ValueError("owner initialization linkage mismatch")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "owner_init_caller": entry["caller"],
            "argument_nonnull": True, "argument_readable": True, "argument_first_word_mapped": False,
            "argument_matches_getter_holder_field": True, "database_init_in_scope": True,
            "catalog_ready_pass": False,
            "limitations": ["argument type and lifetime unresolved", "getter holder captured from a version-bound register",
                            "Linux initialization not attempted", "configuration and Backup unresolved"]}


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
