"""Audit nested lookup observations without exporting pointer or key values."""

import argparse
import json
from pathlib import Path

from audit_finish_lookup import audit as audit_lookup
from audit_finish_lookup_key import compare_key


def compare_inner(android, linux):
    trace = android["trace"]["events"]
    outer_a = [row for row in trace if row.get("kind") == "finish_lookup"]
    inner_a = [row for row in trace if row.get("kind") == "lookup_inner"]
    outer_l = [row for row in linux["events"] if row.get("name") == "finish_lookup"]
    inner_l = [row for row in linux["events"] if row.get("name") == "lookup_inner"]
    if any(len(rows) != 1 for rows in (outer_a, inner_a, outer_l, inner_l)):
        raise ValueError("nested lookup observations incomplete")
    a, i, b, j = outer_a[0], inner_a[0], outer_l[0], inner_l[0]
    before = [row for row in trace if row.get("kind") == "batch_phase" and
              row.get("phase") == "finish_a" and row.get("edge") == "before"]
    if (i.get("batch") != a.get("batch") or i.get("thread") != a.get("thread") or
            len(before) != 1 or type(i.get("sequence")) is not int or
            not before[0]["sequence"] < i["sequence"] < a["sequence"] or
            j.get("thread") != b.get("thread") or type(j.get("return_x0")) is not int or
            type(i.get("nonnull")) is not bool):
        raise ValueError("nested lookup scope or return missing")
    av, bv = a.get("lookupInput"), b.get("lookup_input")
    if not isinstance(av, dict) or not isinstance(bv, dict) or set(av) != {"first_nonnull", "nested_nonnull"} or set(av) != set(bv):
        raise ValueError("lookup input presence missing")
    if any(type(value) is not bool for value in list(av.values()) + list(bv.values())):
        raise ValueError("lookup input presence unresolved")
    return {"android_input": av, "linux_input": bv, "android_inner_nonnull": i["nonnull"],
            "linux_inner_nonnull": j["return_x0"] != 0}


def audit(paths, identities):
    baseline = audit_lookup(paths, identities, allow_inner=True)
    android = json.loads(paths["android"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    return {"schema": "nashura.lookup-inner.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": baseline["matched_update_files"],
            "outer": baseline["comparison"], "key": compare_key(android, linux),
            "inner": compare_inner(android, linux), "catalog_ready_pass": False,
            "limitations": ["pointer presence does not prove object contents are equal",
                            "inner return divergence does not establish a unique cause",
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
