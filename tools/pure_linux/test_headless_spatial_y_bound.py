"""Check the branch encoding and skipped-cell model, not native engine parity."""
from pathlib import Path
import random


def signed32(value):
    value &= 0xffffffff
    return value - 2**32 if value >= 2**31 else value


def visited(height, first, last, skip_tail):
    result = []
    y = first
    while y <= last:
        if y >= 0:
            if height <= y:
                if skip_tail:
                    break
            else:
                result.append(y)
        y += 1
    return result


def test_cell_visitation_order_is_preserved_at_signed_boundaries():
    rng = random.Random(31)
    cases = [(height, first, last) for height in (-3, 0, 1, 2, 31)
             for first in (-32, -1, 0, 1, 31) for last in (-1, 0, 1, 32)]
    cases += [(rng.randint(-10, 64), first, first + rng.randint(0, 64))
              for first in [rng.randint(-64, 64) for _ in range(1000)]]
    for height, first, last in cases:
        assert visited(height, first, last, False) == visited(height, first, last, True)


def test_original_shifted_end_cannot_reach_signed_increment_wrap():
    for center in (-2**31, -2**31 + 1, -1, 0, 1, 2**31 - 1):
        for radius in (-2**31, -1, 0, 1, 2**31 - 1):
            end = signed32(center + radius) >> 10
            assert -2**21 <= end <= 2**21 - 1
            assert end + 1 <= 2**21


def test_single_instruction_retains_condition_and_targets_original_outer_loop():
    before, after = 0x54fffeed, 0x54fffdad
    pc = 0xf34c84
    def target(instruction):
        relative = (instruction >> 5) & 0x7ffff
        if relative & 0x40000:
            relative -= 0x80000
        return pc + 4 * relative
    assert before & 0xff00001f == after & 0xff00001f == 0x5400000d
    assert target(before) == 0xf34c60
    assert target(after) == 0xf34c38
    header = Path(__file__).with_name("headless_spatial_y_bound.h").read_text()
    assert "0x54fffdad" in header
    assert "PROT_READ | PROT_WRITE | PROT_EXEC" not in header
