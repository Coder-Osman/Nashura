"""Verify pinned BL/LDR encodings; native parity remains a runtime gate."""
from pathlib import Path
import re


def sites():
    source = Path(__file__).with_name("headless_hot_getters.h").read_text()
    rows = re.findall(r"\{(0x[0-9a-f]+), (0x[0-9a-f]+), UINT32_C\((0x[0-9a-f]+)\), UINT32_C\((0x[0-9a-f]+)\)\}", source)
    return [tuple(int(value, 16) for value in row) for row in rows]


def test_every_original_call_targets_its_guarded_getter():
    rows = sites()
    assert len(rows) == 11
    assert len({row[0] for row in rows}) == 11
    for pc, target, word, replacement in rows:
        assert word >> 26 == 0b100101  # BL signed imm26
        immediate = word & 0x3ffffff
        if immediate & 0x2000000:
            immediate -= 0x4000000
        assert pc + immediate * 4 == target
        assert replacement & 0x3b000000 == 0x39000000  # unsigned immediate load/store
        assert replacement & 0x00400000  # LDR, not STR
        assert replacement & 0x3ff == 0  # base x0, destination {w,x}0


def test_inlined_load_width_and_offset_are_exact():
    expected = {0xcf0654: (4, 0x188), 0xf1b9e0: (4, 0x11c),
                0xf171b4: (8, 0x48), 0xd98ca4: (8, 0x260)}
    for _, target, _, replacement in sites():
        width = 1 << (replacement >> 30)
        offset = ((replacement >> 10) & 0xfff) * width
        assert (width, offset) == expected[target]


def test_default_and_spatial_candidate_are_not_combined():
    source = Path(__file__).with_name("art_headless_task_bridge.c").read_text()
    assert '? install_spatial_y_bound(base) :' in source
    assert '? install_hot_getters(base) : 0' in source
    header = Path(__file__).with_name("headless_hot_getters.h").read_text()
    assert 'PROT_READ | PROT_WRITE | PROT_EXEC' not in header
    assert 'site->getter + 4' in header
    assert '__builtin___clear_cache' in header
