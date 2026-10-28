"""Audit bounded path histories without publishing private path bytes."""

import argparse
import json
from pathlib import Path

from audit_first_lookup import audit as audit_first


def summarize(rows, key):
    if not isinstance(rows, list) or not isinstance(key, bytes) or not key or len(rows) > 8192:
        raise ValueError("path history or lookup key invalid")
    seen, callers = set(), {}
    calls = returns = prefix_calls = exact_calls = prefix_unique = 0
    prefix = key.split(b"/")[0]
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"caller", "bytes_hex", "calls", "returns"}:
            raise ValueError("path row malformed")
        caller, value, count, completed = (row[name] for name in ("caller", "bytes_hex", "calls", "returns"))
        if (not isinstance(caller, str) or not isinstance(value, str) or len(value) > 8192 or
                type(count) is not int or count <= 0 or type(completed) is not int or completed != count):
            raise ValueError("path row incomplete")
        try:
            data = bytes.fromhex(value)
        except ValueError as error:
            raise ValueError("invalid path bytes") from error
        if data.hex() != value or (caller, value) in seen:
            raise ValueError("noncanonical or duplicate path")
        seen.add((caller, value))
        calls += count
        returns += completed
        callers[caller] = callers.get(caller, 0) + count
        if data.split(b"/")[0] == prefix:
            prefix_calls += count
            prefix_unique += 1
        if data == key:
            exact_calls += count
    return {"unique_pairs": len(rows), "calls": calls, "returns": returns, "callers": callers,
            "matching_first_component_calls": prefix_calls, "matching_first_component_unique_pairs": prefix_unique,
            "exact_lookup_key_calls": exact_calls}


def audit(paths, identities):
    baseline = audit_first(paths, identities, allow_path=True)
    android = json.loads(paths["android"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    events = android["trace"]["events"]
    lookup = [e for e in events if e.get("kind") == "finish_lookup"]
    inner = [e for e in events if e.get("kind") == "lookup_inner"]
    if len(lookup) != 1 or len(inner) != 1 or not isinstance(lookup[0].get("key"), str):
        raise ValueError("lookup evidence missing")
    if linux.get("constructors_covered") is not False:
        raise ValueError("constructor coverage unclear")
    return {"schema": "nashura.path-creation.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": baseline["matched_update_files"],
            "tree": baseline["tree"], "android": summarize(inner[0]["tree"].get("prior_paths"),
                                                            lookup[0]["key"].encode("ascii")),
            "linux_postload_path_calls": 0, "catalog_ready_pass": False,
            "limitations": ["no cross-platform insertion causality proven", "Linux constructors not covered",
                            "Android path entries are captured only before target lookup",
                            "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_before", "android_after", "trace_script", "linux_result", "linux_observation")
    for name in names:
        option = name.replace("_", "-")
        parser.add_argument("--" + option, type=Path, required=True)
        parser.add_argument("--" + option + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit({name: getattr(args, name) for name in names},
                   {name: getattr(args, name + "_sha256") for name in names})
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
