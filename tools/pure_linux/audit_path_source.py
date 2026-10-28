"""Compare observed paths with an identity-bound SQLite resource index."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from audit_first_lookup import audit as audit_first
from audit_path_creation import summarize


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_database(rows, database):
    observed = {bytes.fromhex(row["bytes_hex"]) for row in rows}
    if len(observed) != len(rows):
        raise ValueError("path bytes repeated across caller identities")
    uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        meta = {value.encode("utf-8") for (value,) in connection.execute("SELECT path FROM meta")}
        assets = {value.encode("utf-8") for (value,) in connection.execute("SELECT path FROM asset")}
    return {"observed_unique": len(observed), "meta_unique": len(meta), "asset_unique": len(assets),
            "meta_exact_matches": len(observed & meta), "asset_exact_matches": len(observed & assets),
            "meta_unobserved": len(meta - observed), "observed_not_meta": len(observed - meta)}


def audit(paths, identities, database, database_sha, allow_db=False):
    baseline = audit_first(paths, identities, allow_source=not allow_db, allow_db=allow_db)
    if sha(database) != database_sha:
        raise ValueError("database identity mismatch")
    android = json.loads(paths["android"].read_text())
    result = json.loads(paths["linux_result"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    if (result["resources_before"]["update"].get("assets.scdb") != database_sha or
            linux.get("constructors_covered") is not False):
        raise ValueError("database binding or coverage missing")
    events = android["trace"]["events"]
    lookup = [e for e in events if e.get("kind") == "finish_lookup"]
    inner = [e for e in events if e.get("kind") == "lookup_inner"]
    if len(lookup) != 1 or len(inner) != 1 or not isinstance(lookup[0].get("key"), str):
        raise ValueError("lookup evidence missing")
    rows = inner[0]["tree"].get("prior_source_paths")
    summary = summarize(rows, lookup[0]["key"].encode("ascii"))
    coverage = {item["label"]: item["installed"] for item in android["trace"]["coverage"]}
    parents = inner[0]["tree"].get("source_parents")
    if (coverage.get("source_loader_candidate") is not True or android["trace"].get("activeSourceLoaderScopes") != 0 or
            not isinstance(parents, dict) or any(not isinstance(k, str) or type(v) is not int or v <= 0
                                               for k, v in parents.items()) or sum(parents.values()) != summary["calls"]):
        raise ValueError("source loader scope incomplete")
    comparison = compare_database(rows, database)
    if sha(database) != database_sha:
        raise ValueError("database changed during audit")
    return {"schema": "nashura.path-source.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "database_sha256": database_sha,
            "matched_update_files": baseline["matched_update_files"], "android": summary,
            "android_source_parent_calls": parents,
            "database_comparison": comparison, "linux_postload_source_calls": 0,
            "catalog_ready_pass": False,
            "limitations": ["matching path sets do not prove direct runtime data provenance",
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
