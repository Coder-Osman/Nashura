"""Audit the bounded logical database open and initialization chain."""

import argparse
import json
from pathlib import Path

from audit_path_source import audit as audit_source


def check_chain(android, expected_calls):
    trace = android["trace"]
    events = trace["events"]
    kinds = ("database_open", "database_constructor", "database_init_enter", "database_init_return", "lookup_inner")
    groups = {kind: [e for e in events if e.get("kind") == kind] for kind in kinds}
    if any(len(groups[kind]) != 1 for kind in kinds):
        raise ValueError("database chain coverage incomplete")
    opened, ctor, enter, returned, inner = (groups[kind][0] for kind in kinds)
    if (trace.get("activeDbCtorScopes") != 0 or trace.get("activeDbInitScopes") != 0 or
            opened.get("path") != "assets.scdb" or ctor.get("path") != opened.get("path") or
            opened.get("result_nonnull") is not True or ctor.get("reader_nonnull") is not True or
            type(ctor.get("id")) is not int or opened.get("parent") != ctor["id"] or
            type(enter.get("id")) is not int or enter.get("constructor_id") != ctor["id"] or
            returned.get("id") != enter["id"] or returned.get("result") != 1 or
            any(e.get("thread") != opened.get("thread") for e in (ctor, enter, returned)) or
            not opened["sequence"] < ctor["sequence"] < enter["sequence"] < returned["sequence"] < inner["sequence"]):
        raise ValueError("database chain mismatch")
    if inner.get("tree", {}).get("source_db_init") != {str(enter["id"]): expected_calls}:
        raise ValueError("source calls not bound to initialization scope")
    return {"logical_path_is_assets_scdb": True, "open_result_nonnull": True,
            "reader_nonnull": True, "init_return": 1, "source_calls_in_scope": expected_calls}


def audit(paths, identities, database, database_sha):
    source = audit_source(paths, identities, database, database_sha, allow_db=True)
    android = json.loads(paths["android"].read_text())
    chain = check_chain(android, source["android"]["calls"])
    return {"schema": "nashura.database-init.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "database_sha256": database_sha,
            "matched_update_files": source["matched_update_files"], "android": chain,
            "database_comparison": source["database_comparison"],
            "linux_postload_constructor_calls": 0, "linux_postload_init_calls": 0,
            "linux_postload_loader_calls": 0, "catalog_ready_pass": False,
            "limitations": ["logical open result does not independently prove physical file or bytes read",
                            "Linux constructors are not covered", "configuration and Backup unresolved"]}


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
