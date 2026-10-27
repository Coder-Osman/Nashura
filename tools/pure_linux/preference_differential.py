"""Compare C candidate with interpreted private DEX control flow, not Android runtime."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import subprocess

from preference_dex_vm import DexMachine, SELECTED, parse_methods
from preference_method_audit import method_evidence
from preference_model import KEY, DecodeFailure, PlatformFailure, Storage, configure, encoded, invoke


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(storage, result):
    return {"result": result, "events": storage.events, "memory": storage.memory,
            "disk": storage.disk, "commits": storage.commits, "editors": storage.editors}


def interpreted(methods, storage, operation, value):
    machine = DexMachine(methods, storage)
    try:
        result = machine.execute(SELECTED[2 if operation == "read" else 3],
                                 [KEY] if operation == "read" else [KEY, value])
        outcome = [0, result]
    except (PlatformFailure, DecodeFailure):
        outcome = [-1, None]
    return snapshot(storage, outcome), machine.visited


def candidate(library, storage, operation, value):
    code, output = invoke(library, storage, operation, value)
    return snapshot(storage, [code, output if code == 0 else None])


def states(slot):
    return [{}, {encoded(slot): "cipher:"}, {encoded(slot): "cipher:old"},
            {encoded(slot): "bad"}, {KEY: "legacy"},
            {KEY: "legacy", encoded(slot): "cipher:old"}, {encoded(slot): 123}]


def compare_all(methods, library):
    rows, visited = [], set()
    for case_id, (initial, (operation, value)) in enumerate(itertools.product(
            itertools.product(states(0), states(1)),
            [("read", None), ("write", None), ("write", ""), ("write", "old"), ("write", "new")])):
        initial = list(initial)
        base, _ = interpreted(methods, Storage(initial), operation, value)
        variants = [((), ())] + [((), (number,)) for number in range(1, 5)]
        variants += [((number,), ()) for number in range(1, len(base["events"]) + 1)]
        for failures, false_commits in variants:
            expected, coverage = interpreted(methods, Storage(initial, failures, false_commits), operation, value)
            actual = candidate(library, Storage(initial, failures, false_commits), operation, value)
            visited.update(coverage)
            def hashed(record):
                return digest(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
            rows.append({"case_id": case_id, "failure_event": list(failures),
                         "false_commit": list(false_commits), "equal": expected == actual,
                         "expected_sha256": hashed(expected), "actual_sha256": hashed(actual)})
    return {"cases": len(rows), "mismatches": sum(not row["equal"] for row in rows), "rows": rows,
            "coverage": [{"method": name, "visited_instructions": sum(method == name for method, _ in visited),
                          "total_instructions": len(methods[name]["instructions"])} for name in SELECTED],
            "scope": "DEX control flow with shared synthetic codec/storage/backup boundaries; not ART execution",
            "android_runtime_parity": "NOT_RUN", "catalog_gate": False}


def main():
    parser = argparse.ArgumentParser()
    for name in ("dex", "dexdump", "native-library"):
        parser.add_argument("--" + name, type=Path, required=True)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = {name: getattr(args, name.replace("-", "_")) for name in ("dex", "dexdump", "native-library")}
    identities = {name: digest(path.read_bytes()) for name, path in paths.items()}
    if any(identities[name] != getattr(args, name.replace("-", "_") + "_sha256") for name in paths):
        raise ValueError("input identity mismatch")
    if args.output.exists():
        raise ValueError("output already exists")
    result = subprocess.run([str(args.dexdump), "-d", str(args.dex)], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError("dexdump failed")
    text = result.stdout.decode()
    methods = parse_methods(text)
    evidence = compare_all(methods, configure(args.native_library))
    evidence.update(schema="nashura.preference-differential.v1", inputs=identities,
                    dump_sha256=digest(result.stdout), method_evidence=method_evidence(text, SELECTED))
    if any(digest(path.read_bytes()) != identities[name] for name, path in paths.items()):
        raise ValueError("input changed")
    evidence["inputs_unchanged"] = True
    with args.output.open("x") as stream:
        json.dump(evidence, stream, indent=2)
        stream.write("\n")
    print(json.dumps({key: evidence[key] for key in ("cases", "mismatches", "coverage", "scope")}))
    return 2 if evidence["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
