"""Audit observed resource-database factory and bounded file-open evidence."""

import argparse
import json
from pathlib import Path

from audit_database_init import audit as audit_database


def compare_factory(android):
    trace = android["trace"]
    events = trace["events"]
    kinds = ("engine_db_init_enter", "engine_db_init_return", "database_factory", "database_constructor")
    groups = {kind: [e for e in events if e.get("kind") == kind] for kind in kinds}
    if any(len(groups[kind]) != 1 for kind in kinds):
        raise ValueError("factory chain coverage incomplete")
    enter, returned, factory, ctor = (groups[kind][0] for kind in kinds)
    if (trace.get("activeDbFactoryScopes") != 0 or trace.get("activeEngineDbScopes") != 0 or
            type(enter.get("id")) is not int or returned.get("id") != enter["id"] or
            factory.get("engine_parent") != enter["id"] or ctor.get("factory_parent") != factory.get("id") or
            enter.get("slot_before_nonnull") is not False or returned.get("slot_after_nonnull") is not True or
            factory.get("result_nonnull") is not True or enter.get("owner_is_root") is not False or
            enter.get("owner_is_context") is not False or
            any(e.get("thread") != enter.get("thread") for e in (returned, factory, ctor)) or
            not enter["sequence"] < ctor["sequence"] < factory["sequence"] < returned["sequence"]):
        raise ValueError("factory chain incomplete")
    coverage = {item["label"]: item["installed"] for item in trace["coverage"]}
    if not all(coverage.get(name) is True for name in ("database_factory_candidate", "engine_db_init_candidate", "libc_open", "libc_openat")):
        raise ValueError("file-open hooks incomplete")
    files = [e for e in events if e.get("kind") == "filesystem_open" and e.get("database_parent") == ctor["id"]]
    opens = [e for e in events if e.get("kind") == "database_libc_open" and e.get("parent") == ctor["id"]]
    if (not files or not opens or any(e.get("thread") != ctor.get("thread") or
            e.get("path") != "assets.scdb" or e.get("nonnull") is not True for e in files)):
        raise ValueError("filesystem scope incomplete")
    expected = "/data/user/0/nullsroyale.rel.free/update/assets.scdb"
    if any(e.get("thread") != ctor.get("thread") or e.get("path") != expected or
           type(e.get("fd")) is not int or e["fd"] < 0 or e.get("api") not in ("open", "openat") for e in opens):
        raise ValueError("physical path open mismatch")
    if any(e["sequence"] >= ctor["sequence"] or e["sequence"] <= enter["sequence"] for e in opens + files):
        raise ValueError("physical path order mismatch")
    return {"engine_caller": enter["caller"], "factory_caller": factory["caller"],
            "owner_is_root": False, "owner_is_context": False, "slot_before_nonnull": False,
            "slot_after_nonnull": True, "physical_update_path_open_count": len(opens),
            "filesystem_open_count": len(files), "filesystem_vtables": sorted({e["vtable"] for e in files})}


def audit(paths, identities, database, database_sha):
    base = audit_database(paths, identities, database, database_sha)
    android = json.loads(paths["android"].read_text())
    return {"schema": "nashura.database-factory.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "database_sha256": database_sha,
            "matched_update_files": base["matched_update_files"], "factory": compare_factory(android),
            "database_comparison": base["database_comparison"], "catalog_ready_pass": False,
            "limitations": ["successful open and matching file identity do not trace every read",
                            "Linux constructors not covered", "configuration and Backup unresolved"]}


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
