"""Validate lifecycle boundaries and reject incomplete/tampered diagnostics."""
from pathlib import Path

import pytest

import run_headless_match as runner
from summarize_headless_benchmark import records, summarize as benchmark
from summarize_headless_memory_lifecycle import GROUPS, HEAP_KEYS, PHASES, summarize
from summarize_headless_stability import summarize as stability
from test_headless_stability import sample
from test_run_headless_match import inputs


def fixture(interval=1, text=None):
    text = sample() if text is None else text
    lines, index = [], 0
    metrics = records(text, "HEADLESS_BATTLE_METRICS")
    def observation(phase):
        row = metrics[index]
        start, end = int(row["start_ns"]), int(row["end_ns"])
        time = {"pre_create": start - 60, "pre_step": start - 40,
                "post_step": end + 1, "post_terminal": end + 20, "post_destroy": end + 40}[phase]
        result = [f"HEADLESS_MEMORY_LIFECYCLE scope=diagnostic-only sequence={index} phase={phase} start_ns={time} end_ns={time + 1} "
                  + " ".join(f"{key}={128 + index}" for key in HEAP_KEYS)]
        for group in GROUPS:
            result.append(f"HEADLESS_MEMORY_MAPPING scope=diagnostic-only sequence={index} phase={phase} group={group} maps=1 size_kb=128 rss_kb=64 pss_kb=32 anonymous_kb=16 private_dirty_kb=8")
        return result
    config = f"HEADLESS_MEMORY_LIFECYCLE_CONFIG scope=diagnostic-only interval={interval} allocator_api=mallinfo allocator_scope=process-global classification=pathname-heuristic"
    for line in text.splitlines():
        if line.startswith("HEADLESS_BATTLE_METRICS ") and index % interval == 0:
            if not index:
                lines.append(config)
            for phase in PHASES[:3]:
                lines.extend(observation(phase))
        if line.startswith("HEADLESS_BATTLE_RESET "):
            if index % interval == 0:
                for phase in PHASES[3:]:
                    lines.extend(observation(phase))
            index += 1
        lines.append(line)
    return chr(10).join(lines)


def run(text, interval=1):
    return summarize(text, 2, 2, 3601, "6c959326dd2f09cc", 3091, interval)


@pytest.mark.parametrize("interval,count", [(1, 4), (2, 2), (3, 2), (100, 1)])
def test_complete_diagnostic_has_no_performance_claim(interval, count):
    result = run(fixture(interval), interval)
    assert result["matches"] == 4 and result["sampled_matches"] == count
    assert result["scope"] == "diagnostic-only"
    assert result["benchmark"] is result["whole_goal_complete"] is False
    assert "ticks_per_second" not in result
    assert result["trends"]["post_destroy"]["heap_bytes"]["uordblks_bytes"]["last_minus_first"] == ((3 // interval) * interval)


@pytest.mark.parametrize("old,new", [
    ("scope=diagnostic-only", "scope=benchmark"),
    ("phase=pre_create", "phase=post_destroy"),
    ("sequence=0", "sequence=1"),
    ("group=art_named", "group=unknown"),
    ("arena_bytes=128", "arena_bytes=-1"),
    ("arena_bytes=128", "arena_bytes=128 arena_bytes=129"),
    ("rss_kb=64", "rss_kb=129"),
    ("pss_kb=32", "pss_kb=65"),
    ("anonymous_kb=16", "anonymous_kb=65"),
    ("start_ns=11440", "start_ns=0"),
    ("end_ns=11461", "end_ns=11501"),
    ("start_ns=12501", "start_ns=12499"),
    ("interval=1", "interval=2"),
    ("digest=6c959326dd2f09cc", "digest=0000000000000000"),
])
def test_corrupt_diagnostics_rejected(old, new):
    assert old in fixture()
    with pytest.raises(ValueError):
        run(fixture().replace(old, new, 1))


def test_missing_duplicate_reordered_and_unknown_records_rejected():
    text = fixture()
    lines = text.splitlines()
    position = next(i for i, line in enumerate(lines) if line.startswith("HEADLESS_MEMORY_MAPPING "))
    swapped = list(lines)
    swapped[position], swapped[position + 1] = swapped[position + 1], swapped[position]
    for changed in (lines[:position] + lines[position + 1:], lines + [lines[position]],
                    swapped, lines + ["HEADLESS_MEMORY_LIFECYCLE_UNRECOGNIZED value=1"]):
        with pytest.raises(ValueError):
            run(chr(10).join(changed))


def test_formal_timing_rejects_lifecycle_observation():
    text = fixture()
    with pytest.raises(ValueError, match="diagnostic"):
        benchmark(text, 4)
    with pytest.raises(ValueError, match="diagnostic"):
        stability(text, 2, 2, 3601, "6c959326dd2f09cc", 3091, 0)


def test_runner_diagnostic_is_explicit_and_preserves_default_plan(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.setenv("NASHURA_MEMORY_LIFECYCLE_EVERY", "1")
    ordinary = runner.build_plan(settings, output, benchmark_repeats=20)
    assert "NASHURA_MEMORY_LIFECYCLE_EVERY" not in ordinary["environment"]
    assert "measurement_scope" not in ordinary
    plan = runner.build_plan(settings, output, benchmark_repeats=20, serial_batches=2,
                             memory_lifecycle_every=64)
    assert plan["environment"]["NASHURA_MEMORY_LIFECYCLE_EVERY"] == "64"
    assert plan["measurement_scope"] == "memory-lifecycle-diagnostic-only"
    assert plan["snapshot_trace"] is False and not output.exists()


@pytest.mark.parametrize("kwargs", [{}, {"benchmark_repeats": 20, "thread_resources": True},
    {"benchmark_repeats": 20, "native_phases": True},
    {"benchmark_repeats": 20, "record_hand_actions": True},
    {"benchmark_repeats": 20, "memory_lifecycle_every": 0},
    {"benchmark_repeats": 20, "memory_lifecycle_every": True},
    {"benchmark_repeats": 20, "memory_lifecycle_every": 1.5},
    {"benchmark_repeats": 20, "memory_lifecycle_every": 1000001}])
def test_runner_rejects_mixed_or_invalid_diagnostic(inputs, kwargs):
    settings, output = inputs
    with pytest.raises(ValueError, match="independent repeated diagnostic"):
        runner.build_plan(settings, output, **(dict(memory_lifecycle_every=1) | kwargs))


def test_native_sampling_is_outside_loop_and_after_actual_destructors():
    source = Path(__file__).with_name("art_headless_task_bridge.c").read_text()
    start = source.index("static int run_game_state_battle(")
    stop = source.index("static int inspect_tables(", start)
    source = source[start:stop]
    assert source.index('memory_sequence, "pre_step"') < source.index("uint64_t elapsed_start")
    assert source.index("uint64_t elapsed_ns") < source.index('memory_sequence, "post_step"')
    last = source.index('memory_sequence, "post_terminal"')
    assert source.index('memory_sequence, "post_destroy"') > source.index("release_json_object(root);", last)
    header = Path(__file__).with_name("headless_memory_lifecycle.h").read_text()
    assert "mallopt(" not in header and "malloc_trim(" not in header
    assert "malloc_info(" not in header
