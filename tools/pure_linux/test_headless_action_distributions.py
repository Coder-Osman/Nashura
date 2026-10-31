"""Accounting/tamper tests; synthetic timings are never native evidence."""
from pathlib import Path
import shutil
import subprocess
import pytest

from summarize_headless_actions import summarize
from test_headless_native_phases import sample as native_sample


def sample():
    rows = []
    for index in range(2):
        rows.extend([("injected_parse", 2, 0xd2fe78, index, -1, 1, -1, -1),
                     ("injected_append", 1, 0xd2eef0, index, -1, 1, index, index + 1)])
    rows.extend(("dispatch_mode3", 10, 0xd66b10, -1, tick, 0, -1, -1) for tick in (1, 2))
    native = native_sample().replace(
        "phase=command_dispatch_mode3 calls=2 inclusive_ns=20 exclusive_ns=20 min_ns=10 max_ns=20",
        "phase=command_dispatch_mode3 calls=2 inclusive_ns=20 exclusive_ns=20 min_ns=10 max_ns=10")
    lines = [native,
        "HEADLESS_ACTION_DISTRIBUTION scope=diagnostic-only capacity=4096 samples=6 overflow=0"]
    for sequence, (phase, duration, target, index, tick, result, before, after) in enumerate(rows, 1):
        lines.append(f"HEADLESS_ACTION_SAMPLE scope=diagnostic-only sequence={sequence} phase={phase} duration_ns={duration} target_offset={hex(target)} input_index={index} tick={tick} result={result} queue_before={before} queue_after={after}")
    return chr(10).join(lines)


def test_conservation_and_absent_path_are_explicit():
    result = summarize(sample())
    assert result["sample_count"] == 6
    assert result["phases"]["injected_parse"]["total_ns"] == 4
    assert result["phases"]["dispatch_mode3"]["percentiles_ns"] == {"p50": 10, "p95": 10, "p99": 10}
    assert result["phases"]["dispatch_mode2"]["percentiles_ns"]["p99"] is None
    assert result["benchmark"] is result["whole_goal_complete"] is False


@pytest.mark.parametrize("old,new", [
    ("samples=6", "samples=5"), ("overflow=0", "overflow=1"),
    ("capacity=4096", "capacity=4097"), ("sequence=2", "sequence=1"),
    ("duration_ns=2", "duration_ns=-1"), ("duration_ns=2", "duration_ns=3"),
    ("duration_ns=10", "duration_ns=11"), ("target_offset=0xd2fe78", "target_offset=0xd2eef0"),
    (" target_offset=0xd66b10", " target_offset=0xd66b14"),
    ("tick=2 result=0", "tick=0 result=0"), ("tick=2 result=0", "tick=3 result=0"),
    ("input_index=1", "input_index=0"), ("queue_before=0 queue_after=1", "queue_before=0 queue_after=2"),
    ("tick=2 result=0", "tick=2 result=1"), ("tick=-1 result=1", "tick=0 result=1"),
    ("phase=injected_parse", "phase=unmeasured"),
    ("HEADLESS_ACTION_SAMPLE scope=diagnostic-only", "HEADLESS_ACTION_SAMPLE scope=benchmark"),
])
def test_tampered_samples_rejected(old, new):
    with pytest.raises(ValueError):
        summarize(sample().replace(old, new))


def test_missing_duplicate_and_reordered_rows_rejected():
    lines = sample().splitlines()
    for text in (chr(10).join(lines[:-1]), sample() + chr(10) + lines[-1],
                 chr(10).join(lines[:-6] + [lines[-5], lines[-6]] + lines[-4:]),
                 sample() + chr(10) + lines[-7]):
        with pytest.raises(ValueError):
            summarize(text)


def test_cmd_only_diagnostic_has_no_injected_distribution():
    lines = sample().splitlines()
    lines = [line for line in lines if not line.startswith("HEADLESS_ACTION_SAMPLE") or "dispatch_mode3" in line]
    text = chr(10).join(lines).replace("injected_logic_parse_ns=4 injected_logic_append_ns=2 loaded_commands=0 injected_commands=2",
        "injected_logic_parse_ns=0 injected_logic_append_ns=0 loaded_commands=2 injected_commands=0")
    text = text.replace("samples=6", "samples=2").replace("sequence=5", "sequence=1").replace("sequence=6", "sequence=2")
    result = summarize(text)
    assert result["phases"]["injected_parse"]["calls"] == 0


def test_native_buffer_is_opt_in_bounded_and_released(tmp_path):
    compiler = shutil.which('clang') or shutil.which('cc')
    if compiler is None:
        pytest.skip('host C compiler unavailable')
    source = tmp_path / 'buffer.c'
    source.write_text('''#include "headless_action_distributions.h"
int main(int argc, char **argv) {
    (void)argv;
    if (argc == 1) {
        unsetenv("NASHURA_ACTION_DISTRIBUTIONS");
        if (!action_samples_begin(0) || action_samples) return 1;
        action_sample_add(AS_PARSE, 1, 1, 0, -1, 1, -1, -1);
        if (action_sample_count) return 2;
        setenv("NASHURA_ACTION_DISTRIBUTIONS", "1", 1);
        if (action_samples_begin(0) || !action_samples_begin(1)) return 3;
        if (action_samples_begin(1)) return 4;
        action_sample_add(AS_PARSE, 7, 0xd2fe78, 0, -1, 1, -1, -1);
        action_samples_report();
        if (action_samples || action_sample_count) return 5;
        return 0;
    }
    setenv("NASHURA_ACTION_DISTRIBUTIONS", "1", 1);
    if (!action_samples_begin(1)) return 6;
    for (unsigned i=0; i<=ACTION_SAMPLE_CAPACITY; ++i)
        action_sample_add(AS_MODE3, i, 0xd30a00, -1, 1, 0, -1, -1);
    return 7;
}
''')
    binary = tmp_path / 'buffer'
    subprocess.run([compiler, '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra',
        '-Werror', '-I', str(Path(__file__).parent), str(source), '-o', str(binary)], check=True)
    result = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
    assert 'capacity=4096 samples=1 overflow=0' in result.stdout
    assert 'phase=injected_parse duration_ns=7 target_offset=0xd2fe78' in result.stdout
    overflow = subprocess.run([str(binary), 'overflow'], text=True, capture_output=True)
    assert overflow.returncode == 126 and not overflow.stdout
