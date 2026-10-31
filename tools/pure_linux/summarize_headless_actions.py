"""Reconcile bounded action samples with the original nested diagnostics."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from summarize_headless_benchmark import records
from summarize_headless_native_phases import summarize as summarize_phases

PHASES = ("injected_parse", "injected_append", "dispatch_mode2", "dispatch_mode3")


def summarize(text):
    native = summarize_phases(text)
    headers = records(text, "HEADLESS_ACTION_DISTRIBUTION")
    samples = records(text, "HEADLESS_ACTION_SAMPLE")
    if (native["schema"] != "nashura.headless-native-phases.v2" or len(headers) != 1
            or headers[0].get("scope") != "diagnostic-only"
            or headers[0].get("capacity") != "4096" or headers[0].get("overflow") != "0"
            or not 0 < len(samples) <= 4096 or int(headers[0]["samples"]) != len(samples)):
        raise ValueError("complete bounded diagnostic samples required")
    grouped = {name: [] for name in PHASES}
    for index, row in enumerate(samples, 1):
        if row.get("scope") != "diagnostic-only" or row.get("phase") not in grouped:
            raise ValueError("invalid action sample scope or phase")
        item = {k: int(row[k], 16 if k == "target_offset" else 10) for k in (
            "sequence", "duration_ns", "target_offset", "input_index", "tick", "result",
            "queue_before", "queue_after")}
        if item["sequence"] != index or item["duration_ns"] < 0:
            raise ValueError("ordered nonnegative action samples required")
        grouped[row["phase"]].append(item)
    counts = native["parse_enqueue"]
    injected = counts["injected_commands"]
    if [x["phase"] for x in samples[:2 * injected]] != [
            name for _ in range(injected) for name in PHASES[:2]]:
        raise ValueError("each injected parse must precede its append")
    for name, target, total in (("injected_parse", 0xd2fe78, "injected_logic_parse_ns"),
                                ("injected_append", 0xd2eef0, "injected_logic_append_ns")):
        rows = grouped[name]
        if len(rows) != injected or sum(x["duration_ns"] for x in rows) != counts[total]:
            raise ValueError("per-input duration/count conservation failed")
        for index, row in enumerate(rows):
            expected_queue = counts["loaded_commands"] + index
            if (row["target_offset"] != target or row["input_index"] != index
                    or row["tick"] != -1 or row["result"] != 1
                    or (name == "injected_parse" and (row["queue_before"], row["queue_after"]) != (-1, -1))
                    or (name == "injected_append" and (row["queue_before"], row["queue_after"]) != (expected_queue, expected_queue + 1))):
                raise ValueError("input sample identity or queue transition differs")
    dispatches = samples[2 * injected:]
    if any(x["phase"] not in PHASES[2:] for x in dispatches):
        raise ValueError("dispatch-only step samples required")
    ticks = [int(x["tick"]) for x in dispatches]
    if ticks != sorted(ticks) or any(not 0 <= tick < native["final_tick"] for tick in ticks):
        raise ValueError("ordered in-battle dispatch ticks required")
    for name in PHASES[2:]:
        rows = grouped[name]
        phase = native["phases"]["command_" + name]
        durations = [x["duration_ns"] for x in rows]
        if (len(rows) != phase["calls"] or sum(durations) != phase["inclusive_ns"]
                or sum(x["result"] == 0 for x in rows) != phase["zero_results"]
                or sum(x == 0 for x in durations) != phase["zero_duration_calls"]
                or (durations and (min(durations), max(durations)) != (phase["min_ns"], phase["max_ns"]))
                or sum(x["target_offset"] != int(phase["first_target_offset"], 16) for x in rows) != phase["different_target_calls"]
                or any(x["input_index"] != -1 or (x["queue_before"], x["queue_after"]) != (-1, -1) for x in rows)):
            raise ValueError("dispatch sample counters differ from native windows")
    result = {}
    for name, rows in grouped.items():
        values = sorted(x["duration_ns"] for x in rows)
        result[name] = dict(calls=len(values), total_ns=sum(values),
            min_ns=min(values) if values else None, max_ns=max(values) if values else None,
            median_ns=statistics.median(values) if values else None,
            percentiles_ns={"p" + str(q): values[(len(values) - 1) * q // 100] if values else None
                            for q in (50, 95, 99)},
            percentile_method="sorted floor((n-1)*p/100); no interpolation",
            zero_duration_calls=values.count(0), samples=rows)
    return dict(schema="nashura.headless-action-distributions.v1", scope="diagnostic-only",
        final_tick=native["final_tick"], sample_count=len(samples), phases=result,
        parse_enqueue=counts, benchmark=False, whole_goal_complete=False,
        stdout_sha256=hashlib.sha256(text.encode()).hexdigest(),
        limitations=["clock, wrappers and diagnostic sampling overhead included; no speed comparison",
            "injected parse/append precede step; no online input or cmd parser latency claim",
            "dispatch return values are raw statuses, not proof of action success",
            "small correlated samples and timer quantization; absent paths remain unmeasured"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.log.read_text())
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps(dict(samples=result["sample_count"], benchmark=False)))


if __name__ == "__main__":
    main()
