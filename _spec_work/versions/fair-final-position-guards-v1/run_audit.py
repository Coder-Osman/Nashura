import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import time

from position_guards import ArmPositionOracle, division_certificate, load_code, reference


def cases():
    base = dict(character=True, lower_y=0, upper_x=36, upper_y=64, notify=0, terrain=1)
    boundaries = [-2147483648, -2147483500, -1001, -1000, -999, -501, -500, -499,
        -1, 0, 1, 499, 500, 501, 999, 1000, 17999, 18000, 31999, 32000, 2147483647]
    for coordinate_x in boundaries:
        for coordinate_y in boundaries:
            for character in (False, True):
                for notify, terrain in ((0, 0), (1, 0), (2, 1), (3, 0x80000001)):
                    yield dict(base, x=coordinate_x, y=coordinate_y,
                        character=character, notify=notify, terrain=terrain)
    for offset in (-500, 0, 500):
        for column in range(18):
            for row in range(32):
                for notify, terrain in ((0, 0), (1, 0), (2, 1), (3, 0x80000001)):
                    yield dict(base, x=500 + column * 1000 + offset,
                        y=500 + row * 1000 + offset, notify=notify, terrain=terrain)
    for lower_y in (-2147483648, -1, 0, 1, 2, 32, 2147483647):
        for upper_x, upper_y in ((-1, -2147483648), (0, 0), (1, 1), (36, 64), (2147483647, 2147483647)):
            for coordinate_x, coordinate_y in ((0, 0), (-499, -499), (500, 500), (17500, 31500)):
                yield dict(base, x=coordinate_x, y=coordinate_y,
                    upper_x=upper_x, upper_y=upper_y, lower_y=lower_y)
    generator = random.Random(20261025)
    for _ in range(4096):
        yield dict(x=generator.getrandbits(32), y=generator.getrandbits(32),
            upper_x=generator.getrandbits(32), upper_y=generator.getrandbits(32),
            lower_y=generator.getrandbits(32), character=bool(generator.getrandbits(1)),
            notify=generator.getrandbits(32), terrain=generator.getrandbits(32))


def audit(library):
    started = time.monotonic()
    code, identity = load_code(library)
    oracle = ArmPositionOracle(code)
    digest, counts, visited, calls = hashlib.sha256(), Counter(), set(), Counter()
    for count, case in enumerate(cases(), start=1):
        expected = reference(case)
        observed, observed_calls, observed_visited = oracle.run(case)
        if expected != observed:
            raise ValueError("position helper counterexample: " + json.dumps(dict(case=case, expected=expected, observed=observed)))
        counts[observed] += 1
        calls.update(hex(address) for address in observed_calls)
        visited.update(observed_visited)
        digest.update(json.dumps(dict(case=case, returned=observed, calls=observed_calls),
            sort_keys=True, separators=(",", ":")).encode() + b"\n")
    if len(visited) != identity["instruction_count"] or set(counts) != set(range(6)):
        raise ValueError("position oracle instruction or return-code coverage incomplete")
    return dict(schema="FairFinalPositionGuardAudit/v1", status="passed_conditional_scope",
        identity=identity, division_certificate=division_certificate(), cases=count,
        case_result_sha256=digest.hexdigest(), return_counts=dict(counts),
        instruction_coverage=len(visited), call_counts=dict(calls),
        source_sha256={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("position_guards.py", "run_audit.py")},
        dependencies={name: importlib.metadata.version(name) for name in ("unicorn", "capstone", "lief")},
        seconds=time.monotonic() - started, training_admissible=False, gates_closed=False,
        native_game_reachability_proven=False, terrain_helper_semantics_proven=False,
        full_legal_mask_sufficiency_proven=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise ValueError("position audit output already exists")
    report = audit(arguments.library)
    with arguments.output.open("x") as output:
        output.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
