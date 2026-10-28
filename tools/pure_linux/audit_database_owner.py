"""Audit initialization owner observations and their time-dependent coverage."""

import argparse
import json
from pathlib import Path

from audit_database_factory import audit as audit_factory


def compare_owner(android):
    trace = android["trace"]
    entries = [e for e in trace["events"] if e.get("kind") == "engine_db_init_enter"]
    later = [e for e in trace["events"] if e.get("kind") == "batch_phase" and
             e.get("phase") == "finish_a" and e.get("edge") == "before"]
    if len(entries) != 1 or len(later) != 1:
        raise ValueError("owner comparison incomplete")
    entry, after = entries[0], later[0]
    refs = entry.get("owner_references")
    if (entry.get("root_nonnull") is not False or entry.get("context_nonnull") is not False or
            not isinstance(entry.get("owner_vtable"), str) or not isinstance(refs, dict) or
            set(refs) != {"root", "context", "globals"} or refs["root"] is not None or
            refs["context"] is not None or not isinstance(refs["globals"], list) or
            any(not isinstance(v, str) for v in refs["globals"]) or
            after.get("later_root_nonnull") is not True or after.get("later_context_nonnull") is not True or
            type(after.get("owner_matches_later_root")) is not bool or
            type(after.get("owner_matches_later_context")) is not bool or
            entry["sequence"] >= after["sequence"]):
        raise ValueError("owner observation invalid")
    return {"initial_root_nonnull": False, "initial_context_nonnull": False,
            "root_reference_scan": "NOT_COVERED", "context_reference_scan": "NOT_COVERED",
            "observed_global_pointer_offsets": refs["globals"], "owner_vtable_offset": entry["owner_vtable"],
            "later_root_nonnull": True, "later_context_nonnull": True,
            "owner_address_matches_later_root": after["owner_matches_later_root"],
            "owner_address_matches_later_context": after["owner_matches_later_context"]}


def audit(paths, identities, database, database_sha):
    base = audit_factory(paths, identities, database, database_sha)
    android = json.loads(paths["android"].read_text())
    return {"schema": "nashura.database-owner.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "database_sha256": database_sha,
            "matched_update_files": base["matched_update_files"], "factory": base["factory"],
            "owner": compare_owner(android), "catalog_ready_pass": False,
            "limitations": ["no owner lifetime or allocation trace", "global scan covers writable module ranges only",
                            "later address comparison does not establish lifetime", "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_before", "android_after", "trace_script", "linux_result", "linux_observation")
    for name in names:
        option = name.replace("_", "-")
        parser.add_argument("--" + option, type=Path, required=True)
        parser.add_argument("--" + option + "-sha256", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--database-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit({name: getattr(args, name) for name in names},
                   {name: getattr(args, name + "_sha256") for name in names}, args.database, args.database_sha256)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
