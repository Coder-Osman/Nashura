"""Validate bounded serial tasks inside one initialized native engine process."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from run_headless_match import verify_completion
from summarize_headless_benchmark import summarize as summarize_benchmark


def summarize(text, batches, repeats, expected_tick, expected_digest, expected_bytes, warmup_matches=16):
    if not 2 <= batches <= 240:
        raise ValueError("multiple bounded serial batches required")
    lines = text.splitlines()
    begins = [index for index, line in enumerate(lines) if line.startswith("HEADLESS_SERIAL_BATCH_BEGIN ")]
    ends = [index for index, line in enumerate(lines) if line.startswith("HEADLESS_SERIAL_BATCH_END ")]
    if len(begins) != batches or len(ends) != batches:
        raise ValueError("incomplete or duplicate serial batches")
    creates = [x for x in lines if x.startswith("HEADLESS_CREATE_RETURN_NULL=")]
    if (len(creates) != 1 or creates[0] not in
            ("HEADLESS_CREATE_RETURN_NULL=true", "HEADLESS_CREATE_RETURN_NULL=false")
            or lines.index(creates[0]) >= begins[0]
            or len([x for x in lines[begins[0]:] if x.startswith("HEADLESS_TASK_TID=")]) != batches
            or lines.count("HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1") != batches
            or any(x.startswith(("HEADLESS_NATIVE_PHASE", "HEADLESS_PHASE_TIMING ",
                    "HEADLESS_WARM_RESOURCES ", "HEADLESS_BATTLE_OBSERVATION ",
                    "HEADLESS_BATTLE_OBJECT ", "HEADLESS_BATTLE_PLAYER ")) for x in lines)):
        raise ValueError("wrong engine lifecycle or diagnostics mixed into stability")
    verify_completion(0, text, batches * repeats)
    done = ("HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1", "HEADLESS_REJECTED_PM_CALLS=0",
            "HEADLESS_BATTLE_READY=UNVERIFIED")
    if any(lines.count(marker) != 1 or lines.index(marker) < ends[-1] for marker in done):
        raise ValueError("normal process completion must follow all serial batches")
    results = []
    previous_end = -1
    all_rss = []
    tids = []
    for number, (begin, end) in enumerate(zip(begins, ends), 1):
        if (begin <= previous_end or end <= begin
                or lines[begin] != f"HEADLESS_SERIAL_BATCH_BEGIN index={number} batches={batches}"
                or lines[end] != f"HEADLESS_SERIAL_BATCH_END index={number} batches={batches} status=1"):
            raise ValueError("wrong serial batch order")
        chunk = lines[begin + 1:end]
        if chunk.count("HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1") != 1:
            raise ValueError("serial task did not complete normally")
        batch_tids = [x.split("=", 1)[1] for x in chunk if x.startswith("HEADLESS_TASK_TID=")]
        if len(batch_tids) != 1 or not batch_tids[0].isdigit():
            raise ValueError("one Mainloop thread per serial batch required")
        tids.extend(batch_tids)
        result = summarize_benchmark(chr(10).join(chunk + list(done)), repeats,
            expected_tick=expected_tick, expected_digest=expected_digest,
            expected_bytes=expected_bytes, warmup_matches=warmup_matches)
        if results and result["samples"][0]["start_ns"] <= results[-1]["last_loop_end_ns"]:
            raise ValueError("overlapping serial task clocks")
        rss = [x["rss_kb"] for x in result["memory_samples"]]
        if any(value < 0 for value in rss):
            raise ValueError("missing serial memory sample")
        all_rss.extend(rss)
        result.update(batch=number, first_loop_start_ns=result["samples"][0]["start_ns"],
                      last_loop_end_ns=result["samples"][-1]["end_ns"],
                      rss_first_kib=rss[0], rss_last_kib=rss[-1], rss_peak_kib=max(rss))
        results.append(result)
        previous_end = end
    setup_tids = [x.split("=", 1)[1] for x in lines[:begins[0]] if x.startswith("HEADLESS_TASK_TID=")]
    if len(set(tids + setup_tids)) != 1:
        raise ValueError("Mainloop thread changed within one engine process")
    return {"schema": "nashura.headless-serial-stability.v1",
        "scope": "single process; repeated fixed-input native battles; bounded duration",
        "batches": batches, "matches": batches * repeats,
        "measured_matches": batches * (repeats - warmup_matches),
        "final_tick": expected_tick, "terminal_digest": expected_digest,
        "first_to_last_loop_seconds": (results[-1]["last_loop_end_ns"] - results[0]["first_loop_start_ns"]) / 1e9,
        "measured_loop_seconds": sum(x["timed_seconds"] for x in results),
        "median_of_batch_medians_ms": statistics.median(x["median_match_ms"] for x in results),
        "rss_kib": {"first": all_rss[0], "last": all_rss[-1], "min": min(all_rss),
                    "max": max(all_rss), "last_minus_first": all_rss[-1] - all_rss[0]},
        "batch_summaries": [{key: value for key, value in x.items() if key not in
            ("samples", "memory_samples", "step_sample_phase_by_match", "step_sample_phase_period_by_match")}
            for x in results], "stdout_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "whole_goal_complete": False,
        "limitations": ["fixed fixture only; terminal validation is not full-frame comparison",
            "sampled RSS is not allocation accounting or proof of absence of memory leaks",
            "repeated matches/tasks share one process and are not IID experiments",
            "bounded observed duration; not an indefinite or 24-hour stability claim"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--batches", type=int, required=True)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--expected-tick", type=int, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-bytes", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.log.read_text(), args.batches, args.repeats,
                       args.expected_tick, args.expected_digest, args.expected_bytes)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({key: value for key, value in result.items() if key != "batch_summaries"}, indent=2))


if __name__ == "__main__":
    main()
