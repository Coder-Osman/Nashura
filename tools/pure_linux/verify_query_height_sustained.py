"""Pinned signed-height combination sustained run with two boundary harnesses."""
import argparse
import json
from pathlib import Path

from verify_component_sustained import validate_instruction as validate_component
from verify_component_sustained import verify as verify_sustained
from verify_query_signed_height import HARNESS_SHA256, OBSERVER, OBSERVER_SHA256

GUEST_ROOT = Path("/home/gujiga/nashura-query-height-sustained-20261031-v2")
VARIANTS = {"original": None, "candidate": "hot-distance-component-height-v1"}


def validate_instruction(check, timing):
    # Both instruction paths remain required; their real guest checks bracket
    # all formal timing. A top-level success cannot hide either failed child.
    if check["exit_code"] != 0 or check.get(timing) is not True:
        raise ValueError("two successful boundary harnesses required")
    validate_component(check["component"], timing)
    height = check["signed_height"]
    marker = "QUERY_SIGNED_HEIGHT_CHECK cases=192424 original_and_candidate=equal " \
        "guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3"
    if (height["exit_code"] != 0 or height["stderr"] or height.get(timing) is not True
            or height["harness_sha256"] != HARNESS_SHA256
            or height["stdout"].splitlines().count(marker) != 1):
        raise ValueError("unchanged signed-height boundary harness required")


def verify(root, reference):
    result = verify_sustained(root, reference, variants=VARIANTS, guest_root=GUEST_ROOT,
        observer=OBSERVER, observer_sha256=OBSERVER_SHA256,
        instruction_validator=validate_instruction, schema="nashura.query-height-sustained.v1")
    result["limitations"].append(
        "negative y reads valid height; ordinary complete grids; fixed four action streams")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.reference)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({k: v for k, v in result.items() if k not in ("cases", "post_full")}))


if __name__ == "__main__":
    main()
