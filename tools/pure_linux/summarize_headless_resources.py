"""Summarize opt-in thread resources around independently validated warm loops.

CPU and rusage boundaries enclose the loop but include boundary syscalls. They
do not identify futex/Binder wait causes or hardware cycles/cache behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from run_headless_match import verify_completion
from summarize_headless_benchmark import records, summarize as summarize_benchmark

COUNTERS = ("thread_cpu_ns", "user_us", "system_us", "minor_faults", "major_faults",
            "voluntary_switches", "involuntary_switches", "input_blocks", "output_blocks")
TIMES = ("boundary_start_ns", "loop_start_ns", "loop_end_ns", "boundary_end_ns")


def summarize(text, repeats, expected_tick, expected_digest, expected_bytes, warmup_matches=16):
    verify_completion(0, text, repeats)
    benchmark = summarize_benchmark(text, repeats, expected_tick=expected_tick,
        expected_digest=expected_digest, expected_bytes=expected_bytes, warmup_matches=warmup_matches)
    rows = records(text, "HEADLESS_WARM_RESOURCES")
    if len(rows) != repeats:
        raise ValueError("one resource boundary record per complete match required")
    result = []
    for row, metric in zip(rows, benchmark["samples"]):
        if row.get("scope") != "diagnostic-only" or row.get("valid") != "1":
            raise ValueError("invalid resource diagnostic scope or capture")
        values = {key: int(row[key]) for key in TIMES + COUNTERS}
        if any(value < 0 for value in values.values()):
            raise ValueError("negative resource counter")
        start, loop_start, loop_end, end = (values[key] for key in TIMES)
        if (not start <= loop_start < loop_end <= end
                or loop_start != metric["start_ns"] or loop_end != metric["end_ns"]
                or values["thread_cpu_ns"] > end - start
                or (result and start < result[-1]["boundary_end_ns"])):
            raise ValueError("invalid enclosing resource interval")
        values["boundary_wall_ns"] = end - start
        values["loop_wall_ns"] = loop_end - loop_start
        values["boundary_extra_ns"] = end - start - (loop_end - loop_start)
        result.append(values)
    measured = result[warmup_matches:]
    totals = {key: sum(row[key] for row in measured) for key in
              COUNTERS + ("boundary_wall_ns", "loop_wall_ns", "boundary_extra_ns")}
    return {"schema": "nashura.headless-thread-resources.v1",
        "scope": "independent boundary diagnostic; not baseline speed or exact off-CPU attribution",
        "matches": repeats, "warmup_matches": warmup_matches, "measured_matches": len(measured),
        "final_tick": expected_tick, "terminal_digest": expected_digest,
        "totals": totals,
        "thread_cpu_fraction_of_boundary_wall": totals["thread_cpu_ns"] / totals["boundary_wall_ns"],
        "boundary_wall_minus_thread_cpu_ns": totals["boundary_wall_ns"] - totals["thread_cpu_ns"],
        "matches_with_counters": {key: sum(row[key] > 0 for row in measured) for key in
             ("minor_faults", "major_faults", "voluntary_switches", "involuntary_switches",
              "input_blocks", "output_blocks")},
        "boundary_extra_ns": {"median": statistics.median(row["boundary_extra_ns"] for row in measured),
                               "max": max(row["boundary_extra_ns"] for row in measured)},
        "sampled_memory_kib": {key: {"first_measured": benchmark["memory_samples"][warmup_matches][key],
              "last": benchmark["memory_samples"][-1][key],
              "min": min(row[key] for row in benchmark["memory_samples"][warmup_matches:]),
              "max": max(row[key] for row in benchmark["memory_samples"][warmup_matches:])}
              for key in ("rss_kb", "pss_kb")},
        "records": result, "stdout_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "whole_goal_complete": False,
        "limitations": ["rusage and CPU clocks bracket but do not exactly equal the loop interval",
            "resource values are for Mainloop thread only, not other ART/native threads",
            "wall minus CPU includes scheduling/virtualization/boundary effects; no wait-cause inference",
            "user/system microseconds may be coarsely accounted; not CPU hardware cycles",
            "minor faults are not allocation counts; snapshots do not prove absence of leaks"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--warmup-matches", type=int, default=16)
    parser.add_argument("--expected-tick", type=int, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-bytes", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.log.read_text(), args.repeats, args.expected_tick,
                       args.expected_digest, args.expected_bytes, args.warmup_matches)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
