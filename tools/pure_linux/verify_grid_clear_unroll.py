"""Recompute exact full-frame gates and serial ABBA/BAAB grid-clear timings."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import tempfile

from compare_headless_snapshots import compare, inspect_archive
from run_headless_match import optimization_installation_markers, verify_completion
from summarize_headless_benchmark import records, summarize
from summarize_headless_fourway import CASES, OBSERVATIONS, COMPACT_OMIT, require_equal, sha256
from summarize_headless_threeway import validate_plan, validate_terminal_archive
from verify_combined_followup import validate_actions

VARIANTS = {"original": None, "combined": "hot-distance-v1", "candidate": "hot-distance-grid-v1"}
ORDER = ("combined", "candidate", "candidate", "combined", "candidate", "combined", "combined", "candidate")
GUEST_ROOT = Path("/home/gujiga/nashura-grid-clear-20261031-v1")
OBSERVER = "/dev/shm/nashura-headless-grid-clear-20261031-v1.so"


def read(path):
    return json.loads(Path(path).read_text())


def stdout(directory, variant, *, timing=False, variants=None):
    variants = VARIANTS if variants is None else variants
    if (directory / "stdout.log").exists():
        raise ValueError("only post-exit compressed stdout expected")
    text = gzip.decompress((directory / "stdout.log.gz").read_bytes()).decode()
    markers = [x for x in text.splitlines() if x.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
    expected = optimization_installation_markers(variants[variant]) if variants[variant] else []
    if markers != expected:
        raise ValueError("wrong ordered native installation markers")
    if timing and any(x.startswith(OBSERVATIONS + ("HEADLESS_NATIVE_PHASE", "HEADLESS_PHASE_TIMING ",
                                                  "HEADLESS_WARM_RESOURCES ")) for x in text.splitlines()):
        raise ValueError("diagnostic timing output")
    return text


def result_expected(variant, tick, *, pair=None, variants=None):
    variants = VARIANTS if variants is None else variants
    result = {"status": "NATIVE_FINAL_OBSERVED_LIMITED", "final_tick": tick,
        "snapshot_trace": pair is not None, "matches": 1 if pair else 216,
        "record_hand_actions": False, "native_optimization": variants[variant],
        "native_phases": False, "thread_resources": False,
        "serial_batches": 1, "whole_goal_complete": False}
    if pair:
        result.update(records=pair["records"], archive_bytes=pair["right"]["bytes"],
                      archive_sha256=pair["right"]["sha256"])
    return result


def aggregate(rows):
    if [(x["index"], x["variant"]) for x in rows] != list(enumerate(ORDER, 1)):
        raise ValueError("complete ABBA/BAAB order required")
    def path(rows):
        if any(x["summary"]["matches"] != 216 or x["summary"]["measured_matches"] != 200 for x in rows):
            raise ValueError("216 runs with 16 warmup required")
        return {"batches": len(rows), "batch_median_ms": [x["summary"]["median_match_ms"] for x in rows],
            "median_batch_match_ms": statistics.median(x["summary"]["median_match_ms"] for x in rows),
            "aggregate_ticks_per_second": sum(x["summary"]["measured_matches"] * x["summary"]["tick"] for x in rows) / sum(x["summary"]["timed_seconds"] for x in rows),
            "step_percentiles_us": {k: statistics.median(x["summary"]["step_percentiles_us"][k] for x in rows) for k in ("p50", "p95", "p99")}}
    variants = {k: path([x for x in rows if x["variant"] == k]) for k in ("combined", "candidate")}
    before, after = variants["combined"], variants["candidate"]
    blocks = []
    for block in (rows[:4], rows[4:]):
        a, b = (path([x for x in block if x["variant"] == k]) for k in ("combined", "candidate"))
        blocks.append((b["median_batch_match_ms"] / a["median_batch_match_ms"] - 1) * 100)
    return {"variants": variants,
        "duration_change_percent": (after["median_batch_match_ms"] / before["median_batch_match_ms"] - 1) * 100,
        "throughput_change_percent": (after["aggregate_ticks_per_second"] / before["aggregate_ticks_per_second"] - 1) * 100,
        "block_duration_change_percent": blocks}


def verify(root, reference, *, variants=None, guest_root=None, observer=None,
           schema="nashura.grid-clear-unroll.v1", require_grid_instruction_check=True,
           timing_case_order=None):
    variants = VARIANTS if variants is None else variants
    guest_root = GUEST_ROOT if guest_root is None else Path(guest_root)
    observer = OBSERVER if observer is None else observer
    timing_cases = tuple(CASES if timing_case_order is None else timing_case_order)
    if sorted(timing_cases) != sorted(CASES):
        raise ValueError("timing cases must be a complete permutation")
    root, reference = map(Path, (root, reference))
    if (root / "controller.exit").read_text().strip() != "0":
        raise ValueError("normal controller exit required")
    if read(root / "correctness-done.json") != {"runs": 12, "successes": 12}:
        raise ValueError("12 full-frame runs required")
    done = read(root / "done.json")
    if done["runs"] != 32 or done["successes"] != 32:
        raise ValueError("32 timing batches required")
    if require_grid_instruction_check:
        check = read(root / "instruction-check.json")
        if (check["exit_code"] != 0 or check["stderr"] or
                "GRID_CLEAR_UNROLL_CHECK cases=24107 original_and_candidate=equal guard_pages=passed duplicate_cells=covered signed_modular_product=covered" not in check["stdout"].splitlines()):
            raise ValueError("native instruction equivalence required")
    stage = read(root / "stage-plan.json")
    require_equal(stage["variants"], variants)
    require_equal(stage["cases"], list(CASES))
    require_equal(stage["order"], list(ORDER))
    require_equal(stage.get("timing_case_order", list(CASES)), list(timing_cases))
    if (stage["benchmark_repeats"] != 216 or stage["warmup_matches"] != 16
            or stage["profiling"] is not False or stage["concurrency"] is not False
            or stage["engineering_checks_finished_before_launch"] is not True):
        raise ValueError("wrong experiment stage plan")
    for env in (stage["environment"], done["environment"]):
        if (env["cpu_count"] != 2 or env["affinity"] != [0, 1]
                or env["memory_mib_expected"] != 6144):
            raise ValueError("fixed VM allocation changed")
    gates, timings = (read(root / x) for x in ("correctness-results.json", "timing-results.json"))
    if ([(x["case"], x["variant"]) for x in gates] != [(c, v) for c in CASES for v in variants]
            or [(x["case"], x["index"], x["variant"]) for x in timings] != [(c, i, v) for c in timing_cases for i, v in enumerate(ORDER, 1)]):
        raise ValueError("wrong case/order or missing runs")
    correctness, cases = [], []
    for name in CASES:
        case = root / name
        settings, prior_settings = read(case / "settings.json"), read(reference / name / "settings.json")
        expected = json.loads(json.dumps(prior_settings))
        expected["observer"] = observer
        expected["identities"][observer] = stage["observer_sha256"]
        require_equal(settings, expected)
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + name + ".json")
        if settings["identities"][settings["replay"]] != sha256(fixture):
            raise ValueError("wrong fixed replay")
        replay = read(fixture)
        original = reference / name / "full-original"
        baseline = gzip.decompress((original / "stdout.log.gz").read_bytes()).decode()
        tick = verify_completion(int((original / "exit.txt").read_text()), baseline)
        resets = records(baseline, "HEADLESS_BATTLE_RESET")
        observations = [x for x in baseline.splitlines() if x.startswith(OBSERVATIONS)]
        if len(resets) != 1 or not observations:
            raise ValueError("reference reset and observed states required")
        reset = resets[0]
        with tempfile.TemporaryDirectory(prefix="nashura-grid-reference-") as tmp:
            raw_ref = Path(tmp) / "reference.bin"
            raw_ref.write_bytes(gzip.decompress((original / "snapshots.bin.gz").read_bytes()))
            archive = inspect_archive(raw_ref, tick)
            if archive["sha256"] != read(original / "result.json")["archive_sha256"]:
                raise ValueError("reference archive identity changed")
            validate_terminal_archive(raw_ref, archive, reset)
            for variant in variants:
                directory = case / ("full-" + variant)
                text = stdout(directory, variant, variants=variants)
                validate_actions(text, replay)
                if (verify_completion(int((directory / "exit.txt").read_text()), text) != tick
                        or [x for x in text.splitlines() if x.startswith(OBSERVATIONS)] != observations):
                    raise ValueError("full-frame final tick or observed states differ")
                plan = read(directory / "plan.json")
                prior = read(original / "plan.json")
                expected_plan = json.loads(json.dumps(prior))
                expected_plan["command"] = [x.replace(prior["output"], plan["output"]).replace(prior_settings["observer"], observer) for x in prior["command"]]
                expected_plan["environment"] = {k: v.replace(prior["output"], plan["output"]) for k, v in prior["environment"].items()}
                if variants[variant]:
                    expected_plan["environment"]["NASHURA_NATIVE_OPTIMIZATION"] = variants[variant]
                expected_plan.update(output=plan["output"], identities=settings["identities"], native_optimization=variants[variant])
                require_equal(plan, expected_plan)
                if Path(plan["output"]) != guest_root / name / ("full-" + variant):
                    raise ValueError("wrong full-frame output path")
                raw = Path(tmp) / (variant + ".bin")
                raw.write_bytes(gzip.decompress((directory / "snapshots.bin.gz").read_bytes()))
                pair = compare(raw_ref, raw, tick)
                if not pair["equal"]:
                    raise ValueError("serializer payload differs")
                result = read(directory / "result.json")
                require_equal(result, result_expected(variant, tick, pair=pair, variants=variants))
                gate = next(x for x in gates if (x["case"], x["variant"]) == (name, variant))
                require_equal(gate["result"], result)
                if (gate["snapshot_gzip_sha256"] != sha256(directory / "snapshots.bin.gz")
                        or gate["stdout_gzip_sha256"] != sha256(directory / "stdout.log.gz")
                        or gate["frames"] != pair["records"] or gate["observed_lines"] != len(observations)
                        or gate["full_frames_equal"] is not True or gate["observed_equal"] is not True):
                    raise ValueError("saved correctness summary differs")
                correctness.append({"case": name, "variant": variant, "frames": pair["records"],
                    "archive_sha256": pair["right"]["sha256"], "observed_lines": len(observations),
                    "full_frames_equal": True, "observed_equal": True})
        verified = []
        for row in [x for x in timings if x["case"] == name]:
            label = str(row["index"]).zfill(2) + "-" + row["variant"]
            directory = case / "timing" / label
            text = stdout(directory, row["variant"], timing=True, variants=variants)
            validate_actions(text, replay)
            plan = read(directory / "plan.json")
            validate_plan(plan, settings, row["variant"], Path(plan["output"]), variants=variants,
                          benchmark_repeats=216, compress_stdout=True)
            if Path(plan["output"]) != guest_root / name / "timing" / label:
                raise ValueError("wrong timing output path")
            if verify_completion(int((directory / "exit.txt").read_text()), text, 216) != tick:
                raise ValueError("wrong final tick")
            summary = summarize(text, 216, expected_tick=tick, expected_digest=reset["digest"], expected_bytes=int(reset["bytes"]), warmup_matches=16)
            summary = {k: v for k, v in summary.items() if k not in COMPACT_OMIT}
            require_equal(summary, read(case / "timing" / (label + "-summary.json")))
            require_equal(summary, row["summary"])
            result = read(directory / "result.json")
            require_equal(result, result_expected(row["variant"], tick, variants=variants))
            require_equal(result, row["result"])
            env = read(case / "timing" / (label + "-environment.json"))
            if env["cpu_count"] != 2 or env["affinity"] != [0, 1]:
                raise ValueError("timing CPU allocation changed")
            verified.append({"index": row["index"], "variant": row["variant"], "summary": summary,
                "stdout_sha256": hashlib.sha256(text.encode()).hexdigest(), "plan_sha256": sha256(directory / "plan.json"), "start_epoch_ns": env["epoch_ns"]})
        result = aggregate(verified)
        result.update(case=name, batches=verified)
        cases.append(result)
    starts = [next(c for c in cases if c["case"] == name)["batches"][index - 1]["start_epoch_ns"]
              for name in timing_cases for index in range(1, len(ORDER) + 1)]
    if starts != sorted(set(starts)):
        raise ValueError("timing process starts must follow the declared case/batch order")
    return {"schema": schema, "host_recomputed": True,
        "observer_sha256": stage["observer_sha256"], "instruction_cases": 24107 if require_grid_instruction_check else None,
        "timing_batches": 32, "timing_matches": 6912, "measured_matches": 6400,
        "correctness": correctness, "cases": cases, "whole_goal_complete": False,
        "limitations": ["two ABBA/BAAB blocks per case; no population significance claim",
            "within-batch matches are not IID; sampled step percentiles",
            "host frequency, temperature and VM scheduling are not locked",
            "terminal digest checks during timing; full frames are a separate gate",
            "no hardware cycles/cache, whole-state equivalence or long-duration stability conclusion"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.root, args.reference)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps({"full_frame_gates": len(result["correctness"]), "timing_batches": 32, "whole_goal_complete": False}))

if __name__ == "__main__":
    main()
