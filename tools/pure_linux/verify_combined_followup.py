"""Recompute quiet ABBA/BAAB and longer combined serial runs from raw evidence."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import tempfile

from compare_headless_snapshots import compare, inspect_archive
from run_headless_match import optimization_installation_markers, verify_completion
from summarize_headless_benchmark import records, summarize as benchmark
from summarize_headless_stability import summarize as stability
from summarize_headless_fourway import CASES, OBSERVATIONS, COMPACT_OMIT, require_equal, sha256
from summarize_headless_threeway import validate_plan, validate_terminal_archive

VARIANTS = {"original": None, "combined": "hot-distance-v1"}
ORDER = ("original", "combined", "combined", "original", "combined", "original", "original", "combined")


def read(path):
    return json.loads(Path(path).read_text())


def stdout(directory, variant, *, timing=False):
    if (directory / "stdout.log").exists():
        raise ValueError("expected verified compressed stdout only")
    with gzip.open(directory / "stdout.log.gz", "rt") as source:
        text = source.read()
    markers = [x for x in text.splitlines() if x.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
    expected = optimization_installation_markers(VARIANTS[variant]) if variant != "original" else []
    if markers != expected:
        raise ValueError("wrong ordered installation markers")
    if timing and any(x.startswith(OBSERVATIONS + ("HEADLESS_NATIVE_PHASE", "HEADLESS_PHASE_TIMING ",
                                                  "HEADLESS_WARM_RESOURCES ")) for x in text.splitlines()):
        raise ValueError("diagnostic timing output")
    return text


def compact(summary):
    return {k: v for k, v in summary.items() if k not in COMPACT_OMIT}


def validate_result(result, variant, tick, *, repeats=1, batches=1, archive=None):
    expected = {"status": "NATIVE_FINAL_OBSERVED_LIMITED", "final_tick": tick,
        "snapshot_trace": archive is not None, "matches": repeats * batches,
        "record_hand_actions": False, "native_optimization": VARIANTS[variant],
        "native_phases": False, "thread_resources": False,
        "serial_batches": batches, "whole_goal_complete": False}
    if archive is not None:
        expected.update(records=archive["records"], archive_bytes=archive["right"]["bytes"],
                        archive_sha256=archive["right"]["sha256"])
    require_equal(result, expected)


def validate_actions(text, replay):
    expected = (len(replay["cmd"]), len(replay["inject"]))
    metrics = records(text, "HEADLESS_BATTLE_METRICS")
    if not metrics or any((int(x.get("loaded_commands", -1)),
                           int(x.get("injected_commands", -1))) != expected for x in metrics):
        raise ValueError("fixed action sequence count changed")


def aggregate(rows):
    if [(x["index"], x["variant"]) for x in rows] != list(enumerate(ORDER, 1)):
        raise ValueError("complete ABBA/BAAB order required")
    def path(selected):
        return {"batches": len(selected),
            "batch_median_ms": [x["summary"]["median_match_ms"] for x in selected],
            "median_batch_match_ms": statistics.median(x["summary"]["median_match_ms"] for x in selected),
            "aggregate_ticks_per_second": sum(x["summary"]["measured_matches"] * x["summary"]["tick"] for x in selected) / sum(x["summary"]["timed_seconds"] for x in selected),
            "step_percentiles_us": {k: statistics.median(x["summary"]["step_percentiles_us"][k] for x in selected) for k in ("p50", "p95", "p99")}}
    variants = {k: path([x for x in rows if x["variant"] == k]) for k in VARIANTS}
    before, after = variants["original"], variants["combined"]
    changes = []
    for block in (rows[:4], rows[4:]):
        a, b = (path([x for x in block if x["variant"] == k]) for k in VARIANTS)
        changes.append((b["median_batch_match_ms"] / a["median_batch_match_ms"] - 1) * 100)
    return {"variants": variants, "duration_change_percent": (after["median_batch_match_ms"] / before["median_batch_match_ms"] - 1) * 100,
            "throughput_change_percent": (after["aggregate_ticks_per_second"] / before["aggregate_ticks_per_second"] - 1) * 100,
            "block_duration_change_percent": changes}


def verify(root, reference, previous):
    root, reference, previous = map(Path, (root, reference, previous))
    if (root.parent / "metadata/nashura-combined-followup-control-20261031-v1.exit").read_text().strip() != "0":
        raise ValueError("normal controller exit required")
    for name, expected in (("correctness-done.json", {"runs": 8, "successes": 8}),
                           ("timing-done.json", {"runs": 32, "successes": 32}),
                           ("done.json", {"runs": 4, "successes": 4})):
        if read(root / name) != expected:
            raise ValueError("complete correctness, timing and serial gates required")
    gates, timings, long = (read(root / x) for x in ("correctness-results.json", "timing-results.json", "long-results.json"))
    if ([(x["case"], x["variant"]) for x in gates] != [(c, v) for c in CASES for v in VARIANTS]
            or [(x["case"], x["index"], x["variant"]) for x in timings] != [(c, i, v) for c in CASES for i, v in enumerate(ORDER, 1)]
            or [x["case"] for x in long] != list(CASES) or any("error" in x for x in gates + timings + long)):
        raise ValueError("missing, reordered or failed raw runs")
    correctness, cases, lifetimes = [], [], []
    for name in CASES:
        case = root / name
        settings = read(case / "settings.json")
        require_equal(settings, read(previous / name / "settings.json"))
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + name + ".json")
        if settings["identities"][settings["replay"]] != sha256(fixture):
            raise ValueError("wrong fixed replay")
        replay = read(fixture)
        original = reference / name / "full-frame"
        baseline = (original / "stdout.log").read_text()
        tick = verify_completion(int((original / "exit.txt").read_text()), baseline)
        resets = records(baseline, "HEADLESS_BATTLE_RESET")
        observations = [x for x in baseline.splitlines() if x.startswith(OBSERVATIONS)]
        if len(resets) != 1 or not observations:
            raise ValueError("reference reset and observed state required")
        reset = resets[0]
        validate_terminal_archive(original / "snapshots.bin", inspect_archive(original / "snapshots.bin", tick), reset)
        for variant in VARIANTS:
            directory = case / ("full-" + variant)
            text = stdout(directory, variant)
            if (verify_completion(int((directory / "exit.txt").read_text()), text) != tick
                    or [x for x in text.splitlines() if x.startswith(OBSERVATIONS)] != observations):
                raise ValueError("full-frame tick or observed state mismatch")
            old_variant = "a" if variant == "original" else "d"
            prior = read(previous / name / old_variant / "plan.json")
            plan = read(directory / "plan.json")
            expected = json.loads(json.dumps(prior))
            expected["command"] = [x.replace(prior["output"], plan["output"]) for x in prior["command"]]
            expected["environment"] = {k: v.replace(prior["output"], plan["output"]) for k, v in prior["environment"].items()}
            expected.update(output=plan["output"], stdout_encoding="gzip-after-process-exit")
            require_equal(plan, expected)
            if Path(plan["output"]).parts[-2:] != (name, "full-" + variant):
                raise ValueError("wrong correctness output")
            with tempfile.TemporaryDirectory(prefix="nashura-followup-") as temporary:
                raw = Path(temporary) / "snapshots.bin"
                with gzip.open(directory / "snapshots.bin.gz", "rb") as source, raw.open("xb") as target:
                    shutil.copyfileobj(source, target)
                pair = compare(original / "snapshots.bin", raw, tick)
            result = read(directory / "result.json")
            if not pair["equal"] or pair["right"]["sha256"] != result["archive_sha256"]:
                raise ValueError("serializer frame mismatch")
            validate_result(result, variant, tick, archive=pair)
            gate = next(x for x in gates if (x["case"], x["variant"]) == (name, variant))
            require_equal(gate["result"], result)
            if (gate["snapshot_gzip_sha256"] != sha256(directory / "snapshots.bin.gz")
                    or gate["stdout_gzip_sha256"] != sha256(directory / "stdout.log.gz")
                    or gate["frames"] != pair["records"] or gate["observed_lines"] != len(observations)
                    or gate["full_frames_equal"] is not True or gate["observed_equal"] is not True):
                raise ValueError("correctness evidence changed")
            correctness.append({"case": name, "variant": variant, "frames": pair["records"],
                "archive_sha256": pair["right"]["sha256"], "observed_lines": len(observations),
                "full_frames_equal": True, "observed_equal": True})
        verified = []
        for row in [x for x in timings if x["case"] == name]:
            label = str(row["index"]).zfill(2) + "-" + row["variant"]
            directory = case / "timing" / label
            text = stdout(directory, row["variant"], timing=True)
            validate_actions(text, replay)
            plan = read(directory / "plan.json")
            validate_plan(plan, settings, row["variant"], Path(plan["output"]), variants=VARIANTS,
                          benchmark_repeats=216, compress_stdout=True)
            if Path(plan["output"]).parts[-3:] != (name, "timing", label):
                raise ValueError("wrong timing directory")
            if verify_completion(int((directory / "exit.txt").read_text()), text, 216) != tick:
                raise ValueError("wrong timing final tick")
            summary = compact(benchmark(text, 216, expected_tick=tick, expected_digest=reset["digest"], expected_bytes=int(reset["bytes"]), warmup_matches=16))
            require_equal(summary, read(case / "timing" / (label + "-summary.json")))
            require_equal(summary, row["summary"])
            result = read(directory / "result.json")
            validate_result(result, row["variant"], tick, repeats=216)
            require_equal(result, row["result"])
            env = read(case / "timing" / (label + "-environment.json"))
            if env["cpu_count"] != 2 or env["affinity"] != [0, 1]:
                raise ValueError("VM CPU allocation drift")
            verified.append({"index": row["index"], "variant": row["variant"], "summary": summary,
                "stdout_sha256": hashlib.sha256(text.encode()).hexdigest(), "plan_sha256": sha256(directory / "plan.json"), "start_epoch_ns": env["epoch_ns"]})
        case_result = aggregate(verified)
        case_result.update(case=name, batches=verified)
        cases.append(case_result)
        directory = case / "long-combined"
        text = stdout(directory, "combined", timing=True)
        validate_actions(text, replay)
        plan = read(directory / "plan.json")
        validate_plan(plan, settings, "combined", Path(plan["output"]), variants=VARIANTS,
                      benchmark_repeats=516, serial_batches=24, timeout_seconds=600, compress_stdout=True)
        if Path(plan["output"]).parts[-2:] != (name, "long-combined"):
            raise ValueError("wrong long-run output")
        if verify_completion(int((directory / "exit.txt").read_text()), text, 12384) != tick:
            raise ValueError("wrong long-run final tick")
        summary = stability(text, 24, 516, tick, reset["digest"], int(reset["bytes"]))
        require_equal(summary, read(case / "long-summary.json"))
        row = next(x for x in long if x["case"] == name)
        result = read(directory / "result.json")
        validate_result(result, "combined", tick, repeats=516, batches=24)
        require_equal(result, row["result"])
        require_equal(summary["rss_kib"], row["rss_kib"])
        require_equal(summary["first_to_last_loop_seconds"], row["observed_seconds"])
        if row["matches"] != summary["matches"]:
            raise ValueError("long-run match count mismatch")
        env = read(case / "long-environment.json")
        if env["cpu_count"] != 2 or env["affinity"] != [0, 1]:
            raise ValueError("long-run CPU allocation drift")
        tids = {x.split("=", 1)[1] for x in text.splitlines() if x.startswith("HEADLESS_TASK_TID=")}
        lifetimes.append({"case": name, "summary": summary, "plan_sha256": sha256(directory / "plan.json"),
            "mainloop_tid": int(next(iter(tids))), "start_epoch_ns": env["epoch_ns"]})
    return {"schema": "nashura.combined-followup.v1", "host_recomputed": True,
        "timing_batches": 32, "timing_matches": 6912, "measured_matches": 6400,
        "serial_processes": 4, "serial_batches": 96, "serial_matches": 49536,
        "correctness": correctness, "cases": cases, "lifetimes": lifetimes, "whole_goal_complete": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.reference, args.previous)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps({k: v for k, v in result.items() if k not in ("correctness", "cases", "lifetimes")}))


if __name__ == "__main__":
    main()
