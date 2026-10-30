"""Strict query-pair experiment gates, reusing the full-frame/serial verifier.

No grid instruction-harness result is claimed for this candidate. Exact branch
relocation checks and native complete games are separate evidence surfaces.
"""
import argparse
import json
from pathlib import Path
import re

from verify_grid_clear_unroll import aggregate, read
from verify_grid_clear_unroll import verify as verify_serial_candidate

VARIANTS = {"original": None, "combined": "hot-distance-v1", "candidate": "hot-distance-query-v1"}
GUEST_ROOT = Path("/home/gujiga/nashura-query-pair-20261031-v1")
OBSERVER = "/dev/shm/nashura-headless-query-pair-20261031-v1.so"


def verify(root, reference):
    check = read(Path(root) / "instruction-check.json")
    lines = check["stdout"].splitlines()
    matches = [re.fullmatch(r"QUERY_DIMENSION_PAIR_CHECK cases=(\d+) original_and_candidate=equal "
        r"guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3",
        line) for line in lines]
    matches = [match for match in matches if match]
    if (check["exit_code"] != 0 or check["stderr"] or len(matches) != 1
            or int(matches[0][1]) != 192424 or check["after_formal_timing"] is not True):
        raise ValueError("native query instruction equivalence required after timing")
    result = verify_serial_candidate(root, reference, variants=VARIANTS,
        guest_root=GUEST_ROOT, observer=OBSERVER,
        schema="nashura.query-dimension-pair.v1", require_grid_instruction_check=False)
    result["instruction_cases"] = int(matches[0][1])
    result["limitations"].append("height read occurs before width/y rejection; valid stable serial grid only")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.root, args.reference)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps({"full_frame_gates": len(result["correctness"]),
        "timing_batches": result["timing_batches"], "whole_goal_complete": False}))


if __name__ == "__main__":
    main()
