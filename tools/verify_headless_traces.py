"""Independently compare private real-native boundary captures without publishing them."""

import argparse
import gzip
from itertools import zip_longest
import json
from pathlib import Path

from headless_native import digest, object_sha, semantic_capture


def verify(left, right):
    bases = ({}, {})
    boundary_rows = []
    previous_tick = -1
    terminal = None
    with gzip.open(left, "rt") as first, gzip.open(right, "rt") as second:
        for line_number, (first_line, second_line) in enumerate(zip_longest(first, second), 1):
            if first_line is None or second_line is None:
                raise ValueError("trace boundary count differs")
            captures = [json.loads(first_line), json.loads(second_line)]
            normalized = [semantic_capture(capture, base) for capture, base in zip(captures, bases)]
            if normalized[0] != normalized[1]:
                raise ValueError("native boundary mismatch at line " + str(line_number))
            capture = normalized[0]
            tick = capture["tick"]
            if tick <= previous_tick or capture["ordinary"]["tick"] != tick:
                raise ValueError("nonmonotonic or inconsistent capture tick")
            previous_tick = tick
            if capture["rich"]["combatEvents"].get("droppedCount", 0):
                raise ValueError("event evidence overflow")
            boundary_rows.append({"tick": tick, "sha256": object_sha(capture)})
            terminal = capture["ordinary"]
    if not boundary_rows or boundary_rows[0]["tick"] != 0:
        raise ValueError("trace must include the reset boundary")
    if not terminal.get("ended") or not terminal.get("finalized") or terminal.get("queuedCommands"):
        raise ValueError("trace lacks a complete native terminal")
    return {"schema": "headless-trace-verification.v1", "status": "passed",
            "boundary_count": len(boundary_rows), "terminal_tick": terminal["tick"],
            "trace_sha256": object_sha(boundary_rows),
            "input_sha256": [digest(left), digest(right)],
            "normalization": ["generation/stateEpoch identity removal",
                              "combat/remaining/phase epoch-relative event sequence and cause",
                              "deployment sequence first-id offset, preserving references"],
            "training_admissible": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.left, args.right)
    with args.output.open("x") as target:
        target.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
