"""Audit first tree lookup using matched real-run identity and scope evidence."""

import argparse
import json
from pathlib import Path

from audit_finish_lookup import audit as audit_lookup
from audit_finish_lookup_key import compare_key
from audit_lookup_inner import compare_inner


def compare_first(android, linux):
    events_a = android["trace"]["events"]
    outer = [e for e in events_a if e.get("kind") == "lookup_inner"]
    found_a = [e for e in events_a if e.get("kind") == "first_lookup_result"]
    inner_l = [e for e in linux["events"] if e.get("name") == "lookup_inner"]
    found_l = [e for e in linux["events"] if e.get("name") == "first_lookup_result"]
    if len(outer) != 1 or not found_a or len(inner_l) != 1 or len(found_l) != 1:
        raise ValueError("tree observation coverage incomplete")
    if any(e.get("thread") != outer[0].get("thread") or e.get("batch") != outer[0].get("batch") or
           type(e.get("found")) is not bool or type(e.get("sequence")) is not int or
           e["sequence"] >= outer[0]["sequence"] for e in found_a):
        raise ValueError("Android tree scope incomplete")
    if [e["sequence"] for e in found_a] != sorted(e["sequence"] for e in found_a):
        raise ValueError("Android tree order invalid")
    tree_a, tree_l = outer[0].get("tree"), inner_l[0].get("first_lookup_tree")
    if not isinstance(tree_a, dict) or not isinstance(tree_l, dict):
        raise ValueError("tree summary missing")
    if (type(tree_a.get("root_nonnull")) is not bool or type(tree_l.get("root_nonnull")) is not bool or
            not isinstance(tree_a.get("size_word"), str) or not tree_a["size_word"].isdigit() or
            type(tree_l.get("size_word")) is not int or tree_l["size_word"] < 0 or
            type(found_l[0].get("first_lookup_found")) is not bool or
            found_l[0].get("thread") != inner_l[0].get("thread")):
        raise ValueError("tree summary incomplete")
    return {"android_root_nonnull": tree_a["root_nonnull"], "android_size_word": int(tree_a["size_word"]),
            "android_first_found": found_a[0]["found"], "android_lookup_count": len(found_a),
            "linux_root_nonnull": tree_l["root_nonnull"], "linux_size_word": tree_l["size_word"],
            "linux_first_found": found_l[0]["first_lookup_found"]}


def audit(paths, identities, allow_population=False, allow_path=False):
    baseline = audit_lookup(paths, identities, allow_inner=True, allow_first=True, allow_population=allow_population, allow_path=allow_path)
    android = json.loads(paths["android"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    return {"schema": "nashura.first-lookup.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": baseline["matched_update_files"],
            "outer": baseline["comparison"], "key": compare_key(android, linux),
            "inner": compare_inner(android, linux), "tree": compare_first(android, linux),
            "catalog_ready_pass": False,
            "limitations": ["tree size word semantics and population source unverified",
                            "separate processes and runtimes", "configuration and Backup unresolved"]}


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
