"""Check balanced batch statistics and reject altered timing evidence."""
from copy import deepcopy
import json
import struct

import pytest

from compare_headless_snapshots import inspect_archive
import run_headless_match as runner
import summarize_headless_threeway as threeway
from test_run_headless_match import inputs


def batch_rows():
    rows = []
    for block, order in enumerate(threeway.ORDERS, 1):
        for position, variant in enumerate(order, 1):
            duration = {"a": 100, "b": 95, "c": 90}[variant] + block
            rows.append({"block": block, "position": position, "variant": variant,
                "summary": {"matches": 116, "measured_matches": 100, "tick": 3601,
                    "timed_seconds": duration / 10, "median_match_ms": duration,
                    "step_percentiles_us": {"p50": 1, "p95": 2, "p99": 3},
                    "sampled_rss_peak_mib": 128}})
    return rows


def test_aggregate_uses_batches_and_paired_blocks():
    result = threeway.aggregate_batches(batch_rows())
    assert result["variants"]["c"]["measured_matches"] == 600
    assert result["variants"]["c"]["matches"] == 696
    assert result["variants"]["c"]["median_of_batch_medians_ms"] == 93.5
    assert result["variants"]["c"]["aggregate_ticks_per_second"] == pytest.approx(2160600 / 56.1)
    comparison = result["comparisons"]["b_to_c"]
    assert comparison["faster_blocks"] == 6
    assert comparison["median_duration_change_percent"] == pytest.approx((93.5 / 98.5 - 1) * 100)
    assert comparison["paired_block_duration_change_percent"][0] == pytest.approx((91 / 96 - 1) * 100)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_block", "wrong_variant"])
def test_reject_incomplete_or_nonunique_batch_sets(mutation):
    rows = batch_rows()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif mutation == "wrong_block":
        rows[-1]["block"] = 7
    else:
        rows[-1]["variant"] = "d"
    with pytest.raises(ValueError):
        threeway.aggregate_batches(rows)


def test_unavailable_memory_is_null():
    rows = batch_rows()
    for row in rows:
        row["summary"]["sampled_rss_peak_mib"] = None
    assert threeway.aggregate_batches(rows)["variants"]["a"]["sampled_rss_peak_mib"] is None


@pytest.mark.parametrize("variant", threeway.VARIANTS)
def test_exact_quiet_plan_matches_runner(inputs, monkeypatch, variant):
    settings, output = inputs
    monkeypatch.setattr(threeway, "ENGINE_SHA256", runner.ENGINE_SHA256)
    plan = runner.build_plan(settings, output, benchmark_repeats=116, timeout_seconds=120,
                             native_optimization=threeway.VARIANTS[variant])
    threeway.validate_plan(plan, settings, variant, output)


@pytest.mark.parametrize("mutation", ["trace", "phases", "profiler", "timeout", "replay",
                                     "unquiet", "identities", "optimization", "output"])
def test_reject_plan_drift(inputs, monkeypatch, mutation):
    settings, output = inputs
    monkeypatch.setattr(threeway, "ENGINE_SHA256", runner.ENGINE_SHA256)
    plan = runner.build_plan(settings, output, benchmark_repeats=116, timeout_seconds=120)
    if mutation == "trace":
        plan["environment"]["NASHURA_TRACE_SNAPSHOTS"] = "/tmp/trace"
    elif mutation == "phases":
        plan["native_phases"] = True
    elif mutation == "profiler":
        plan["command"] = ["perf", "record"] + plan["command"]
    elif mutation == "timeout":
        plan["command"][3] = "45s"
    elif mutation == "replay":
        plan["command"][-2] = "/oldroot/tmp/other-replay.json"
    elif mutation == "unquiet":
        plan["environment"]["NASHURA_QUIET"] = "0"
    elif mutation == "identities":
        plan["identities"] = {}
    elif mutation == "optimization":
        plan["environment"]["NASHURA_NATIVE_OPTIMIZATION"] = "component-getters-v1"
    else:
        plan["output"] = "/tmp/other-batch"
    with pytest.raises(ValueError):
        threeway.validate_plan(plan, settings, "a", output)


def test_terminal_identity_is_derived_from_actual_archived_bytes(tmp_path):
    path = tmp_path / "snapshots.bin"
    path.write_bytes(struct.pack("<ii", 0, 1) + b"x" + struct.pack("<ii", 1, 3) + b"abc")
    archive = inspect_archive(path, 1)
    reset = {"digest": "e16801510db89efd", "bytes": "3"}
    threeway.validate_terminal_archive(path, archive, reset)
    with pytest.raises(ValueError, match="terminal identity"):
        threeway.validate_terminal_archive(path, archive, dict(reset, digest="0" * 16))
    with pytest.raises(ValueError, match="terminal identity"):
        threeway.validate_terminal_archive(path, archive, dict(reset, bytes="4"))


def test_missing_correctness_gate_is_not_replaced_by_benchmark_success(tmp_path):
    root = tmp_path / "correctness"
    root.mkdir()
    (root / "done.json").write_text(json.dumps({"cases": 4, "successes": 3}))
    with pytest.raises(ValueError, match="full-frame correctness"):
        threeway.summarize_root(tmp_path, root)


@pytest.mark.parametrize("mutation", ["saved", "compact", "missing", "extra"])
def test_saved_or_controller_summary_tampering_is_rejected(mutation):
    raw = {"median_match_ms": 100, "samples": [1], "memory_samples": [2],
           "step_sample_phase_by_match": [0], "step_sample_phase_period_by_match": [16]}
    saved = deepcopy(raw)
    compact = {"median_match_ms": 100}
    threeway.validate_saved_summary(raw, saved, compact)
    if mutation == "saved":
        saved["samples"] = []
    elif mutation == "compact":
        compact["median_match_ms"] = 99
    elif mutation == "missing":
        compact.clear()
    else:
        compact["unverified"] = True
    with pytest.raises(ValueError, match="raw log"):
        threeway.validate_saved_summary(raw, saved, compact)
