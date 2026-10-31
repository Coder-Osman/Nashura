"""Exact signed-height candidate gates with serial full-frame and timing proof."""
import argparse
import json
from pathlib import Path
from summarize_headless_fourway import CASES

from verify_grid_clear_unroll import read, verify as verify_serial

VARIANTS = {"original": None, "combined": "hot-distance-component-v1",
            "candidate": "hot-distance-component-height-v1"}
GUEST_ROOT = Path("/home/gujiga/nashura-query-height-20261031-v1")
CONFIRM_GUEST_ROOT = Path("/home/gujiga/nashura-query-height-confirm-20261031-v1")
OBSERVER = "/dev/shm/nashura-headless-query-height-20261031-v2.so"
OBSERVER_SHA256 = "fd7991e7e957b5448b475572332777a7448b19325186033daf8b5e5e374786e8"
HARNESS_SHA256 = "c81f867124e6e65eafd385804004c932e8b6e061e176ba883ffcdd048976004e"


def verify(root, reference, *, confirmation=False):
    if read(Path(root) / "stage-plan.json")["observer_sha256"] != OBSERVER_SHA256:
        raise ValueError("pinned signed-height observer required")
    checks = []
    for filename, when in (("instruction-precheck.json", "before_formal_timing"),
                           ("instruction-check.json", "after_formal_timing")):
        check = read(Path(root) / filename)
        marker = "QUERY_SIGNED_HEIGHT_CHECK cases=192424 original_and_candidate=equal " \
            "guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3"
        if (check["exit_code"] != 0 or check["stderr"] or check.get(when) is not True
                or check["stdout"].splitlines().count(marker) != 1
                or check["harness_sha256"] != HARNESS_SHA256):
            raise ValueError("native signed-height boundary checks before and after required")
        checks.append(check)
    if checks[0]["harness_sha256"] != checks[1]["harness_sha256"]:
        raise ValueError("unchanged native harness required")
    result = verify_serial(root, reference, variants=VARIANTS,
        guest_root=CONFIRM_GUEST_ROOT if confirmation else GUEST_ROOT,
        observer=OBSERVER, schema="nashura.query-signed-height.v1",
        require_grid_instruction_check=False,
        timing_case_order=tuple(reversed(CASES)) if confirmation else None)
    if confirmation:
        result.update(schema="nashura.query-signed-height-confirmation.v1", timing_case_order=list(reversed(CASES)))
    result.update(instruction_cases=192424, instruction_checks_before_and_after=True,
        instruction_harness_sha256=checks[0]["harness_sha256"])
    result["limitations"].append("negative y reads valid height; rejected w9/NZCV dead; ordinary complete grid only")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirmation", action="store_true")
    args = parser.parse_args()
    result = verify(args.root, args.reference, confirmation=args.confirmation)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps(dict(full_frame_gates=len(result["correctness"]), timing_batches=32, whole_goal_complete=False)))


if __name__ == "__main__":
    main()
