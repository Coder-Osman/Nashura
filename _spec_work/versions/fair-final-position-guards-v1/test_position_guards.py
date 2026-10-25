from pathlib import Path
import random
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from position_guards import ArmPositionOracle, division_certificate, load_code, reference, signed32


LIBRARY = Path(__file__).parents[1] / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"


def case(**changes):
    return dict(dict(character=True, x=500, y=500, lower_y=0,
        upper_x=36, upper_y=64, terrain=1, notify=0), **changes)


@pytest.mark.parametrize("changes,expected", [
    ({"character": False, "x": -2147483648}, 0),
    ({"x": -500}, 1), ({"x": -499}, 0),
    ({"y": -500}, 2), ({"y": -499}, 0),
    ({"x": 18000}, 3), ({"y": 32000}, 4),
    ({"lower_y": -2147483648, "upper_y": 2147483647}, 4),
    ({"terrain": 0x80000000}, 5), ({"terrain": 0x80000001}, 0),
])
def test_reference_signed_boundaries_and_branch_priority(changes, expected):
    assert reference(case(**changes)) == expected


def test_reciprocal_certificate_and_machine_division_extremes():
    certificate = division_certificate()
    assert certificate["residual"] == 28
    assert certificate["residual_upper"] < certificate["scale"]
    values = [-2147483648, -2147483647, -1000, -500, -499, -1, 0, 1, 499, 500, 2147483647]
    values.extend(random.Random(72).sample(range(-2147483648, 2147483648), 4096))
    for value in values:
        native = ((value * certificate["multiplier"]) >> certificate["shift"]) + int(value < 0)
        expected = value // 500 if value >= 0 else -((-value) // 500)
        assert native == expected


@pytest.mark.parametrize("arguments", [dict(multiplier=274877906), dict(shift=36), dict(divisor=1000)])
def test_invalid_reciprocal_certificate_rejected(arguments):
    with pytest.raises(ValueError, match="certificate"):
        division_certificate(**arguments)


def test_wrong_library_rejected_before_decoding(tmp_path):
    path = tmp_path / "wrong.so"
    path.write_bytes(b"not the engine")
    with pytest.raises(ValueError, match="SHA"):
        load_code(path)


@pytest.fixture
def oracle():
    pytest.importorskip("unicorn")
    if not LIBRARY.exists():
        pytest.skip("private frozen libg resource not present")
    code, identity = load_code(LIBRARY)
    assert identity["instruction_count"] == 81
    return ArmPositionOracle(code)


def test_arm_return_codes_notifications_and_callee_saved_registers(oracle):
    inputs = [case(), case(character=False), case(x=-500), case(y=-500),
        case(x=18000), case(y=32000), case(terrain=0), case(terrain=0, notify=1),
        case(terrain=2, notify=2), case(terrain=2, notify=3)]
    results = [oracle.run(entry)[0] for entry in inputs]
    assert results == [0, 0, 1, 2, 3, 4, 5, 5, 5, 5]


def test_arm_random_signed_inputs_and_wrapped_bounds(oracle):
    generator = random.Random(109)
    for _ in range(128):
        entry = case(**{name: signed32(generator.getrandbits(32))
            for name in ("x", "y", "lower_y", "upper_x", "upper_y", "notify", "terrain")})
        assert oracle.run(entry)[0] == reference(entry)


def test_cpu_oracle_exposes_wrong_reference(oracle):
    observed, _, _ = oracle.run(case(x=18000))
    assert observed != 0
