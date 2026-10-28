"""Audit candidate tree population in bounded, identity-matched real runs."""

import argparse
import json
from pathlib import Path

from audit_first_lookup import audit as audit_first


def history(value):
    if not isinstance(value, dict) or set(value) != {"calls", "returns", "callers"}:
        raise ValueError("population history incomplete")
    calls, returns, callers = value["calls"], value["returns"], value["callers"]
    if (type(calls) is not int or type(returns) is not int or calls < 0 or returns != calls or
            not isinstance(callers, dict) or any(not isinstance(k, str) or type(v) is not int or v <= 0
                                               for k, v in callers.items()) or sum(callers.values()) != calls):
        raise ValueError("population history invalid")
    return value


def audit(paths, identities):
    baseline = audit_first(paths, identities, allow_population=True)
    android = json.loads(paths["android"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    events = [event for event in android["trace"]["events"] if event.get("kind") == "lookup_inner"]
    if len(events) != 1:
        raise ValueError("missing lookup scope")
    tree = events[0]["tree"]
    insert = history(tree.get("prior_insertion"))
    population = history(tree.get("prior_population"))
    allocation = history(tree.get("prior_allocation"))
    if linux.get("constructors_covered") is not False:
        raise ValueError("Linux constructor coverage unclear")
    return {"schema": "nashura.tree-population.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": baseline["matched_update_files"],
            "tree": baseline["tree"], "android_prior_insertion": insert,
            "android_prior_population": population, "android_prior_allocation": allocation,
            "linux_postload_insert_calls": 0, "linux_postload_population_calls": 0,
            "linux_postload_node_calls": 0, "catalog_ready_pass": False,
            "limitations": ["Android observation begins when module observer installs hooks",
                            "Linux constructors are outside observation window",
                            "counts do not prove unique cause or whole-lifecycle absence",
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
