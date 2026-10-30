"""Branch/ABI encoding and accounting checks; full parity is a native gate."""
from pathlib import Path
import re

import pytest

from summarize_headless_native_phases import PHASES, summarize


def sample():
    lines = [
        "HEADLESS_NATIVE_PHASE_INSTALL installed=1 sites=11 scope=diagnostic-only",
        "HEADLESS_GAME_STATE_STEP_RETURN tick_before=0 tick_after=3 completed=3 requested=10000 finalized=1",
        "HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1",
        "HEADLESS_REJECTED_PM_CALLS=0",
        "HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1",
        "HEADLESS_PHASE_TIMING scope=diagnostic-only battle_json_parse_ns=12 game_state_load_ns=20 injected_logic_parse_ns=4 injected_logic_append_ns=2 loaded_commands=0 injected_commands=2 queue_at_step_start=2 queue_at_end=0 queue_removed=2 queue_added=0 queue_change_steps=1 first_queue_change_tick=1 last_queue_change_tick=1",
    ]
    targets = ("0x1062274", "0xcef550", "0xf25a58", "0xd2f37c", "0", "0xd66b10", "0x17e4d60", "0x17e4b40", "0xf6594c")
    counts = (3, 3, 3, 3, 0, 2, 3, 3, 2)
    inclusive = (500, 300, 270, 60, 0, 20, 30, 30, 60)
    exclusive = (140, 30, 150, 40, 0, 20, 30, 30, 60)
    for name, target, count, total, own in zip(PHASES, targets, counts, inclusive, exclusive):
        lines.append(f"HEADLESS_NATIVE_PHASE scope=diagnostic-only phase={name} calls={count} inclusive_ns={total} exclusive_ns={own} min_ns={10 if count else 0} max_ns={total if count else 0} zero_results={count} zero_duration_calls=0 first_target_offset={target} different_target_calls=0 requested_bytes={24 if name == "object_array_new" else 0} max_request_bytes={8 if name == "object_array_new" else 0} valid=1")
    return "\n".join(lines)


def test_validate_complete_diagnostic_with_explicit_scope():
    result = summarize(sample())
    assert result["scope"] == "diagnostic-only"
    assert result["dispatch_calls"] == result["zero_dispatch_results"] == 2
    assert result["benchmark"] is result["whole_goal_complete"] is False
    assert result["phases"]["command_dispatch_mode2"]["mean_inclusive_ns"] is None


@pytest.mark.parametrize("old,new", [
    ("sites=11", "sites=10"),
    ("phase=command_queue calls=3", "phase=command_queue calls=-1"),
    ("phase=game_state_update calls=3", "phase=game_state_update calls=2"),
    ("first_target_offset=0xd2f37c", "first_target_offset=0xd2f380"),
    ("valid=1", "valid=0"),
    ("queue_removed=2", "queue_removed=1"),
    ("injected_commands=2", "injected_commands=0"),
    ("exclusive_ns=30", "exclusive_ns=31"),
    ("exclusive_ns=40", "exclusive_ns=41"),
    ("min_ns=10", "min_ns=0"),
    ("scope=diagnostic-only", "scope=benchmark"),
    ("first_target_offset=0x17e4d60", "first_target_offset=0x17e4b40"),
    ("requested_bytes=24", "requested_bytes=4"),
])
def test_reject_inconsistent_diagnostic_records(old, new):
    with pytest.raises(ValueError):
        summarize(sample().replace(old, new))


def test_reject_duplicate_phase_or_optimization():
    text = sample()
    for extra in (text.splitlines()[-1],
                  "HEADLESS_NATIVE_OPTIMIZATION name=hot-getters-v1 installed=1 sites=11",
                  "HEADLESS_NATIVE_PHASE_INSTALL_ERROR stage=near-island",
                  "HEADLESS_BENCHMARK_METRICS elapsed_ns=1"):
        with pytest.raises(ValueError):
            summarize(text + "\n" + extra)


def test_guarded_sites_preserve_direct_targets_and_tail_branch():
    source = Path(__file__).with_name("headless_native_phases.h").read_text()
    rows = re.findall(r"\{(0x[0-9a-f]+), (0x[0-9a-f]+), \(uintptr_t\)(\w+), ([01])\}", source)
    assert len(rows) == 11
    targets = {0x1062144: 0x1062274, 0x10623e8: 0xcef550,
               0x1062378: 0xd2f37c, 0x10625d8: 0xd2f37c,
               0xf26458: 0x17e4d60, 0xf2676c: 0x17e4b40}
    for pc, word, _, indirect in rows:
        pc, word = int(pc, 16), int(word, 16)
        if indirect == "1":
            assert word == 0xd63f0100  # BLR x8
        else:
            immediate = word & 0x3ffffff
            if immediate & 0x2000000:
                immediate -= 0x4000000
            assert pc + immediate * 4 == targets[pc]
            assert word >> 26 == (5 if pc in (0x10625d8, 0xf2676c) else 37)
    assert "MAP_PRIVATE | MAP_ANONYMOUS" in source
    assert "PROT_READ | PROT_WRITE | PROT_EXEC" not in source
    assert "0xaa0803e4" in source  # mov x4,x8 before wrapper


def test_stub_literal_and_branch_encodings():
    assert (0x58000070 >> 5 & 0x7ffff) * 4 == 12  # indirect stub PC+4 -> literal+16
    assert (0x58000050 >> 5 & 0x7ffff) * 4 == 8
    assert 0xaa0803e4 & 31 == 4
    assert (0xaa0803e4 >> 16) & 31 == 8


def test_clock_quantization_preserves_zero_duration_calls():
    text = sample().replace("min_ns=10", "min_ns=0").replace(
        "zero_duration_calls=0 first_target_offset=0x", "zero_duration_calls=1 first_target_offset=0x")
    result = summarize(text)
    assert result["phases"]["command_queue"]["zero_duration_calls"] == 1
    with pytest.raises(ValueError):
        summarize(text.replace("zero_duration_calls=1", "zero_duration_calls=4"))


def test_legacy_six_phase_logs_keep_their_schema_and_fields():
    lines = sample().replace("sites=11", "sites=8").splitlines()
    lines = [line for line in lines if not any("phase=" + name + " " in line for name in PHASES[6:])]
    text = "\n".join(lines).replace("phase=object_collection_tick calls=3 inclusive_ns=270 exclusive_ns=150",
                                    "phase=object_collection_tick calls=3 inclusive_ns=270 exclusive_ns=270")
    text = re.sub(r" requested_bytes=\d+ max_request_bytes=\d+", "", text)
    result = summarize(text)
    assert result["schema"] == "nashura.headless-native-phases.v1"
    assert list(result["phases"]) == list(PHASES[:6])
    assert "requested_bytes" not in result["phases"]["command_queue"]
    with pytest.raises(ValueError):
        summarize(text.replace("sites=8", "sites=11"))
