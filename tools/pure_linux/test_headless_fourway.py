"""Balanced composition evidence must reject missing, altered or noisy runs."""
from copy import deepcopy
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct

import pytest

import run_headless_match as runner
import summarize_headless_fourway as fourway
import summarize_headless_threeway as threeway
from test_run_headless_match import inputs, COMPLETE


def batches():
    rows = []
    for block, order in enumerate(fourway.ORDERS, 1):
        for position, variant in enumerate(order, 1):
            duration = {"a": 100, "b": 95, "c": 97, "d": 92}[variant] + block
            rows.append({"block": block, "position": position, "variant": variant,
                "summary": {"matches": 116, "measured_matches": 100, "tick": 3601,
                    "timed_seconds": duration / 10, "median_match_ms": duration,
                    "step_percentiles_us": {"p50": 1, "p95": 2, "p99": 3},
                    "sampled_rss_peak_mib": 128}})
    return rows


def test_williams_orders_balance_position_and_every_directed_predecessor():
    assert [set(order) for order in fourway.ORDERS] == [set("abcd")] * 4
    for position in range(4):
        assert {order[position] for order in fourway.ORDERS} == set("abcd")
    predecessors = Counter(pair for order in fourway.ORDERS for pair in zip(order, order[1:]))
    assert predecessors == Counter({(a, b): 1 for a in "abcd" for b in "abcd" if a != b})


def test_batch_aggregation_and_combined_increment_are_not_added_percentages():
    result = fourway.aggregate_batches(batches())
    combined = result["variants"]["d"]
    assert combined["batches"] == 4
    assert combined["matches"] == 464 and combined["measured_matches"] == 400
    assert combined["median_of_batch_medians_ms"] == 94.5
    assert combined["aggregate_ticks_per_second"] == pytest.approx(1440400 / 37.8)
    assert result["comparisons"]["b_to_d"]["median_duration_change_percent"] == pytest.approx(
        (94.5 / 97.5 - 1) * 100)
    assert result["comparisons"]["c_to_d"]["faster_blocks"] == 4
    assert result["duration_interaction_ms_by_block"] == [0] * 4
    assert result["median_duration_interaction_ms"] == 0


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reorder", "position",
                                      "variant", "block", "matches", "warmup"])
def test_incomplete_or_unbalanced_measurements_are_rejected(mutation):
    rows = batches()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif mutation == "reorder":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation in ("position", "variant", "block"):
        rows[-1][mutation] = {"position": 1, "variant": "a", "block": 1}[mutation]
    else:
        rows[-1]["summary"]["matches" if mutation == "matches" else "measured_matches"] = 115
    with pytest.raises(ValueError):
        fourway.aggregate_batches(rows)


@pytest.mark.parametrize("variant", fourway.VARIANTS)
def test_four_variants_use_the_existing_exact_quiet_plan(inputs, monkeypatch, variant):
    settings, output = inputs
    monkeypatch.setattr(threeway, "ENGINE_SHA256", runner.ENGINE_SHA256)
    plan = runner.build_plan(settings, output, benchmark_repeats=116, timeout_seconds=120,
                             native_optimization=fourway.VARIANTS[variant])
    fourway.validate_plan(plan, settings, variant, output, variants=fourway.VARIANTS)
    fourway.validate_markers(chr(10).join(runner.optimization_installation_markers(
        fourway.VARIANTS[variant])) if variant != "a" else "", variant, timing=True)


@pytest.mark.parametrize("change", ["duplicate", "missing", "reversed", "observations", "resources", "phases"])
def test_combined_installation_and_quiet_output_are_strict(change):
    lines = runner.optimization_installation_markers("hot-distance-v1")
    if change == "duplicate":
        lines.append(lines[-1])
    elif change == "missing":
        lines.pop()
    elif change == "reversed":
        lines.reverse()
    else:
        lines.append({"observations": "HEADLESS_BATTLE_OBJECT id=1",
                      "resources": "HEADLESS_WARM_RESOURCES cpu=1",
                      "phases": "HEADLESS_NATIVE_PHASE name=state"}[change])
    with pytest.raises(ValueError):
        fourway.validate_markers(chr(10).join(lines), "d", timing=True)


def test_unavailable_memory_is_not_zero():
    rows = batches()
    for row in rows:
        row["summary"]["sampled_rss_peak_mib"] = None
    assert fourway.aggregate_batches(rows)["variants"]["d"]["sampled_rss_peak_mib"] is None


@pytest.mark.parametrize("value", [1.001, math.nan, math.inf, -math.inf, True, "1"])
def test_raw_summary_comparison_rejects_drift_nonfinite_and_type_changes(value):
    with pytest.raises(ValueError):
        fourway.require_equal({"duration": 1.0}, {"duration": value})


def test_cross_python_float_last_bit_tolerance_keeps_structure_exact():
    fourway.require_equal({"duration": [1.0]}, {"duration": [1.0 + 1e-15]})
    for altered in ({"duration": []}, {}, {"duration": [1.0], "extra": 1}):
        with pytest.raises(ValueError):
            fourway.require_equal({"duration": [1.0]}, altered)


def test_timing_done_does_not_replace_full_frame_gates(tmp_path):
    root = tmp_path / "experiment"
    root.mkdir()
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata/nashura-hot-distance-control-20261031-v1.exit").write_text("0")
    (root / "correctness-done.json").write_text('{"runs":16,"successes":15}')
    with pytest.raises(ValueError, match="full-frame"):
        fourway.summarize_root(root, tmp_path)


def synthetic_evidence(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.setattr(threeway, "ENGINE_SHA256", runner.ENGINE_SHA256)
    root, reference = output.parent / "experiment", output.parent / "reference"
    root.mkdir(); reference.mkdir()
    (root.parent / "metadata").mkdir()
    (root.parent / "metadata/nashura-hot-distance-control-20261031-v1.exit").write_text("0")
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    archive_bytes = b"".join(struct.pack("<ii", tick, 3) + b"abc" for tick in range(18))
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    observed = "HEADLESS_BATTLE_OBJECT id=1\n"
    complete = COMPLETE.replace("3601", "17")
    reset = "HEADLESS_BATTLE_RESET iteration=1 repeats=1 digest=e16801510db89efd bytes=3 equal=1 validated=1"
    gates, rows = [], []
    for case in fourway.CASES:
        before = deepcopy(settings)
        replay = output.parent / (case + "-replay.json")
        fixture = Path(fourway.__file__).with_name("fixtures") / ("headless-runtime-" + case + ".json")
        replay.write_bytes(fixture.read_bytes())
        before["replay"] = str(replay)
        before["identities"][str(replay)] = fourway.sha256(replay)
        current = deepcopy(before)
        observer = output.parent / (case + "-observer.so")
        observer.write_bytes(b"new synthetic observer")
        current["observer"] = str(observer)
        current["identities"][str(observer)] = fourway.sha256(observer)
        save(root / case / "settings.json", current)
        save(reference / case / "settings.json", before)
        original = reference / case / "full-frame"
        original_plan = runner.build_plan(before, original)
        save(original / "plan.json", original_plan)
        (original / "stdout.log").write_text(observed + reset + chr(10) + complete)
        (original / "exit.txt").write_text("0")
        (original / "snapshots.bin").write_bytes(archive_bytes)
        for variant in fourway.VARIANTS:
            directory = root / case / variant
            plan = runner.build_plan(current, directory, native_optimization=fourway.VARIANTS[variant])
            save(directory / "plan.json", plan)
            marker = chr(10).join(runner.optimization_installation_markers(fourway.VARIANTS[variant])) if variant != "a" else ""
            (directory / "stdout.log").write_text(observed + reset + chr(10) + complete + chr(10) + marker)
            (directory / "exit.txt").write_text("0")
            raw = directory / "snapshots.bin"
            raw.write_bytes(archive_bytes)
            pair = fourway.compare(original / "snapshots.bin", raw, 17)
            result = {"final_tick": 17, "records": 18, "archive_sha256": archive_sha, "archive_bytes": len(archive_bytes)}
            save(directory / "result.json", result)
            packed = directory / "snapshots.bin.gz"
            packed.write_bytes(gzip.compress(archive_bytes))
            gates.append({"case": case, "variant": variant, "result": result, "comparison": pair,
                "compressed_snapshot_bytes": packed.stat().st_size,
                "compressed_snapshot_sha256": fourway.sha256(packed),
                "full_frames_equal": True, "observed_equal": True})
        for block, order in enumerate(fourway.ORDERS, 1):
            for position, variant in enumerate(order, 1):
                label = str(block) + "-" + str(position) + "-" + variant
                directory = root / case / "timing" / label
                plan = runner.build_plan(current, directory, benchmark_repeats=116, timeout_seconds=120,
                                         native_optimization=fourway.VARIANTS[variant])
                save(directory / "plan.json", plan)
                logs = []
                for iteration in range(1, 117):
                    phase = (iteration - 1) % 16
                    samples = (17 - phase + 15) // 16
                    start = iteration * 2000
                    logs.extend([f"HEADLESS_BATTLE_METRICS completed=17 wall_ns=1000 step_total_ns=900 step_min_ns=1 step_max_ns=90 p50_ns=5 p95_ns=20 p99_ns=70 peak_rss_kb=2048 trace=0 start_ns={start} end_ns={start + 1000} sample_count={samples} sample_every=16 sample_phase={phase} sample_phase_period=16 loaded_commands=52 injected_commands=0",
                        "HEADLESS_BATTLE_MEMORY rss_kb=2048 pss_kb=1024", complete.splitlines()[0],
                        f"HEADLESS_BATTLE_RESET iteration={iteration} repeats=116 digest=e16801510db89efd bytes=3 equal=1 validated=1"])
                logs.extend(complete.splitlines()[1:] + ["HEADLESS_BATTLE_READY=UNVERIFIED"])
                if variant != "a":
                    logs.extend(runner.optimization_installation_markers(fourway.VARIANTS[variant]))
                log = chr(10).join(logs)
                (directory / "stdout.log").write_text(log)
                (directory / "exit.txt").write_text("0")
                result = {"matches":116, "native_optimization":fourway.VARIANTS[variant]}
                save(directory / "result.json", result)
                summary = fourway.summarize(log, 116, expected_tick=17, expected_digest="e16801510db89efd", expected_bytes=3, warmup_matches=16)
                compact = {k:v for k,v in summary.items() if k not in fourway.COMPACT_OMIT}
                save(directory.parent / (label + "-summary.json"), compact)
                save(directory.parent / (label + "-environment.json"), {"cpu_count":2,"affinity":[0,1]})
                rows.append({"case":case,"block":block,"position":position,"variant":variant,"result":result,"summary":compact})
    save(root / "correctness-results.json", gates)
    save(root / "results.json", rows)
    save(root / "correctness-done.json", {"runs":16,"successes":16})
    save(root / "done.json", {"runs":64,"successes":64})
    return root, reference


def test_complete_synthetic_evidence_and_corrupted_snapshot_rejection(inputs, monkeypatch):
    root, reference = synthetic_evidence(inputs, monkeypatch)
    result = fourway.summarize_root(root, reference)
    assert len(result["correctness"]) == 16 and len(result["cases"]) == 4
    assert result["total_matches"] == 7424 and result["measured_matches"] == 6400
    assert result["whole_goal_complete"] is False
    plan_file = root / fourway.CASES[0] / "d/plan.json"
    original_plan = plan_file.read_text()
    altered_plan = json.loads(original_plan)
    altered_plan["environment"]["NASHURA_NATIVE_PHASES"] = "1"
    plan_file.write_text(json.dumps(altered_plan))
    with pytest.raises(ValueError, match="saved evidence"):
        fourway.summarize_root(root, reference)
    plan_file.write_text(original_plan)
    controller_exit = root.parent / "metadata/nashura-hot-distance-control-20261031-v1.exit"
    controller_exit.write_text("70")
    with pytest.raises(ValueError, match="controller"):
        fourway.summarize_root(root, reference)
    controller_exit.write_text("0")
    packed = root / fourway.CASES[0] / "d/snapshots.bin.gz"
    original = packed.read_bytes()
    data = bytearray(gzip.decompress(original)); data[9] ^= 1
    packed.write_bytes(gzip.compress(data))
    with pytest.raises(ValueError, match="serializer frame"):
        fourway.summarize_root(root, reference)
    packed.write_bytes(original)
    timing = root / fourway.CASES[0] / "timing/1-1-a/stdout.log"
    timing.write_text(timing.read_text().replace("loaded_commands=52", "loaded_commands=0", 1))
    with pytest.raises(ValueError, match="action sequence"):
        fourway.summarize_root(root, reference)
