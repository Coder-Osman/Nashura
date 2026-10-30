"""Exact full-frame, side-effect fallback and serial performance gates."""
import argparse
import json
from pathlib import Path
import re
from verify_grid_clear_unroll import read, verify as verify_serial_candidate
from summarize_headless_fourway import CASES

VARIANTS = {"original": None, "combined": "hot-distance-v1",
            "candidate": "hot-distance-component-v1"}
GUEST_ROOT = Path("/home/gujiga/nashura-component-guard-20261031-v1")
OBSERVER = "/dev/shm/nashura-headless-component-guard-20261031-v1.so"
CONFIRM_GUEST_ROOT = Path("/home/gujiga/nashura-component-confirm-20261031-v1")
OBSERVER_SHA256 = "896d9f20cb415dacc0a1fb4149cd7fed80055ebc5a2024277864e4b5d1bd1892"


def verify(root, reference, *, confirmation=False):
    if confirmation and read(Path(root) / "stage-plan.json")["observer_sha256"] != OBSERVER_SHA256:
        raise ValueError("confirmation requires the unchanged first-stage observer")
    checks = []
    for filename, timing in (("instruction-precheck.json", "before_formal_timing"),
                             ("instruction-check.json", "after_formal_timing")):
        check = read(Path(root) / filename)
        matches = [re.fullmatch(r"COMPONENT_GUARD_CHECK cases=(\d+) original_and_candidate=equal "
            r"guard_pages=passed callee_saved=passed side_effect_fallback=passed matched=(\d+) "
            r"fallback=(\d+) null=(\d+) rejected=(\d+)", line)
            for line in check["stdout"].splitlines()]
        matches = [m for m in matches if m]
        if (check["exit_code"] != 0 or check["stderr"] or check.get(timing) is not True
                or len(matches) != 1 or tuple(map(int, matches[0].groups()))
                != (120000, 4593, 18433, 4617, 92357)):
            raise ValueError("real ARM64 return/ABI/side-effect equivalence required")
        checks.append(check)
    if checks[0]["harness_sha256"] != checks[1]["harness_sha256"]:
        raise ValueError("unchanged instruction harness required")
    result = verify_serial_candidate(root, reference, variants=VARIANTS,
        guest_root=CONFIRM_GUEST_ROOT if confirmation else GUEST_ROOT, observer=OBSERVER,
        schema="nashura.component-guard-confirmation.v1" if confirmation else "nashura.component-guard.v1",
        require_grid_instruction_check=False,
        timing_case_order=tuple(reversed(CASES)) if confirmation else None)
    if confirmation:
        result["timing_case_order"] = list(reversed(CASES))
    result["instruction_cases"] = 120000
    result["instruction_checks_before_and_after"] = True
    result["limitations"].append("valid complete ordinary-memory objects and serial Mainloop; "
        "omitted-read/fault/signal/profiler behavior and concurrent mutation excluded")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirmation", action="store_true")
    args = parser.parse_args()
    result = verify(args.root, args.reference, confirmation=args.confirmation)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps({"full_frame_gates": len(result["correctness"]),
        "timing_batches": result["timing_batches"], "whole_goal_complete": False}))


if __name__ == "__main__":
    main()
