"""Summarize bounded serial latency and RSS/PSS trends from validated raw logs.

Window statistics describe one shared process, not independent replicates or
causal speedups. Memory samples occur outside the timed step loops.
"""
import statistics

from summarize_headless_benchmark import records
from summarize_headless_stability import summarize as stability


def summarize(text, batches, repeats, expected_tick, expected_digest, expected_bytes,
              warmup_matches=16):
    result = stability(text, batches, repeats, expected_tick, expected_digest,
                       expected_bytes, warmup_matches)
    if batches < 3:
        raise ValueError("at least three serial batches required for time windows")
    metrics = [{k: int(v) for k, v in row.items()}
               for row in records(text, "HEADLESS_BATTLE_METRICS")]
    memory = [{k: int(v) for k, v in row.items()}
              for row in records(text, "HEADLESS_BATTLE_MEMORY")]
    if any(row.get(key, -1) <= 0 for row in memory for key in ("rss_kb", "pss_kb")):
        raise ValueError("complete positive RSS and PSS samples required")
    width = batches // 3
    windows = {}
    for label, first, stop in (("early", 0, width), ("middle", width, 2 * width),
                               ("late", 2 * width, batches)):
        indexes = [batch * repeats + match for batch in range(first, stop)
                   for match in range(warmup_matches, repeats)]
        selected = [metrics[i] for i in indexes]
        mem = [memory[i] for i in indexes]
        seconds = sum(row["wall_ns"] for row in selected) / 1e9
        windows[label] = {"first_batch": first + 1, "last_batch": stop,
            "measured_matches": len(indexes), "timed_seconds": seconds,
            "median_match_ms": statistics.median(row["wall_ns"] for row in selected) / 1e6,
            "ticks_per_second": len(indexes) * expected_tick / seconds,
            "step_percentiles_us": {key: statistics.median(row[key + "_ns"]
                for row in selected) / 1000 for key in ("p50", "p95", "p99")},
            "memory_median_kib": {key: statistics.median(row[key + "_kb"]
                for row in mem) for key in ("rss", "pss")}}
    memory_trends = {}
    for key in ("rss", "pss"):
        values = [row[key + "_kb"] for row in memory]
        medians = [statistics.median(values[i * repeats:(i + 1) * repeats])
                   for i in range(batches)]
        times = [(metrics[i * repeats]["start_ns"] - metrics[0]["start_ns"]) / 1e9
                 for i in range(batches)]
        slope, _ = statistics.linear_regression(times, medians)
        memory_trends[key] = {"samples": len(values), "first_kib": values[0],
            "last_kib": values[-1], "min_kib": min(values), "max_kib": max(values),
            "last_minus_first_kib": values[-1] - values[0],
            "batch_median_kib": medians,
            "ols_batch_median_kib_per_second": slope,
            "late_minus_early_median_kib": windows["late"]["memory_median_kib"][key]
                - windows["early"]["memory_median_kib"][key]}
    result.update(schema="nashura.headless-sustained-trends.v1", windows=windows,
        memory_trends=memory_trends,
        late_vs_early_duration_percent=(windows["late"]["median_match_ms"]
            / windows["early"]["median_match_ms"] - 1) * 100)
    result["limitations"].extend([
        "early/middle/late windows are contiguous task partitions, not independent trials",
        "step percentiles are medians of per-match rotating-phase sample percentiles",
        "OLS memory slope is descriptive only; no leak-free or stationarity threshold is inferred",
        "no causal speedup comparison against older sessions"])
    return result
