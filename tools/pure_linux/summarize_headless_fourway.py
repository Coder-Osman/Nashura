"""Recompute fixed original/getter/distance/combined serial evidence.

All sixteen compressed full-frame gates and all 64 raw timing logs are required.
Four balanced blocks are the comparison units; repeated matches are not IID.
"""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import tempfile

from compare_headless_snapshots import compare, inspect_archive
from run_headless_match import optimization_installation_markers, verify_completion
from summarize_headless_benchmark import records, summarize
from summarize_headless_threeway import validate_plan, validate_terminal_archive

ORDERS = ("abdc", "bcad", "cdba", "dacb")
VARIANTS = {"a": None, "b": "hot-getters-v1",
            "c": "saturated-distance-v1", "d": "hot-distance-v1"}
CASES = tuple("deck-" + deck + "-seed-" + seed
              for deck in ("a", "b") for seed in ("1", "7"))
OBSERVATIONS = ("HEADLESS_BATTLE_OBSERVATION ", "HEADLESS_BATTLE_PLAYER ",
                "HEADLESS_BATTLE_OBJECT ")
COMPACT_OMIT = {"samples", "memory_samples", "step_sample_phase_by_match",
                "step_sample_phase_period_by_match"}


def read(path):
    return json.loads(Path(path).read_text())


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def require_equal(actual, expected):
    if isinstance(actual, dict) and isinstance(expected, dict):
        equal = actual.keys() == expected.keys()
        if equal:
            for key in actual:
                require_equal(actual[key], expected[key])
    elif isinstance(actual, list) and isinstance(expected, list):
        equal = len(actual) == len(expected)
        if equal:
            for left, right in zip(actual, expected):
                require_equal(left, right)
    elif type(actual) is float and type(expected) in (int, float):
        equal = math.isfinite(actual) and math.isfinite(expected) and math.isclose(
            actual, expected, rel_tol=1e-14, abs_tol=1e-12)
    else:
        equal = type(actual) is type(expected) and actual == expected
    if not equal:
        raise ValueError("saved evidence differs from recomputed raw evidence")


def validate_markers(text, variant, *, timing=False):
    markers = [line for line in text.splitlines()
               if line.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
    expected = optimization_installation_markers(VARIANTS[variant]) if variant != "a" else []
    if markers != expected:
        raise ValueError("missing or unordered optimization evidence")
    if timing and any(line.startswith(OBSERVATIONS + ("HEADLESS_NATIVE_PHASE",
            "HEADLESS_PHASE_TIMING ", "HEADLESS_WARM_RESOURCES ")) for line in text.splitlines()):
        raise ValueError("diagnostic timing output")


def aggregate_batches(rows):
    expected = [(block, position, variant) for block, order in enumerate(ORDERS, 1)
                for position, variant in enumerate(order, 1)]
    if [(x["block"], x["position"], x["variant"]) for x in rows] != expected:
        raise ValueError("complete balanced four-path order required")
    by_key = {(x["block"], x["variant"]): x["summary"] for x in rows}
    variants = {}
    for name, optimization in VARIANTS.items():
        batches = [by_key[block, name] for block in range(1, 5)]
        if any((x["matches"], x["measured_matches"]) != (116, 100) for x in batches):
            raise ValueError("wrong batch measurement count")
        seconds = sum(x["timed_seconds"] for x in batches)
        variants[name] = {"optimization": optimization, "batches": 4,
            "matches": 464, "measured_matches": 400,
            "batch_median_ms": [x["median_match_ms"] for x in batches],
            "median_of_batch_medians_ms": statistics.median(x["median_match_ms"] for x in batches),
            "aggregate_ticks_per_second": sum(100 * x["tick"] for x in batches) / seconds,
            "step_percentiles_us": {k: statistics.median(x["step_percentiles_us"][k] for x in batches)
                                    for k in ("p50", "p95", "p99")},
            "sampled_rss_peak_mib": max((x["sampled_rss_peak_mib"] for x in batches
                if x["sampled_rss_peak_mib"] is not None), default=None)}
    comparisons = {}
    for before, after in (("a", "b"), ("a", "c"), ("a", "d"), ("b", "d"), ("c", "d")):
        changes = [(by_key[block, after]["median_match_ms"] /
                    by_key[block, before]["median_match_ms"] - 1) * 100 for block in range(1, 5)]
        comparisons[before + "_to_" + after] = {
            "median_duration_change_percent": (variants[after]["median_of_batch_medians_ms"] /
                variants[before]["median_of_batch_medians_ms"] - 1) * 100,
            "aggregate_throughput_change_percent": (variants[after]["aggregate_ticks_per_second"] /
                variants[before]["aggregate_ticks_per_second"] - 1) * 100,
            "paired_block_duration_change_percent": changes,
            "faster_blocks": sum(x < 0 for x in changes)}
    interactions = [by_key[b, "d"]["median_match_ms"] - by_key[b, "b"]["median_match_ms"] -
                    by_key[b, "c"]["median_match_ms"] + by_key[b, "a"]["median_match_ms"]
                    for b in range(1, 5)]
    return {"variants": variants, "comparisons": comparisons,
            "duration_interaction_ms_by_block": interactions,
            "median_duration_interaction_ms": statistics.median(interactions)}


def summarize_root(root, reference):
    root, reference = Path(root), Path(reference)
    exit_file = root.parent / "metadata/nashura-hot-distance-control-20261031-v1.exit"
    if exit_file.read_text().strip() != "0":
        raise ValueError("controller must finish normally")
    if read(root / "correctness-done.json") != {"runs": 16, "successes": 16}:
        raise ValueError("sixteen full-frame gates required")
    rows, gates = read(root / "results.json"), read(root / "correctness-results.json")
    if (read(root / "done.json") != {"runs": 64, "successes": 64} or len(rows) != 64
            or len(gates) != 16 or any("error" in x for x in rows + gates)):
        raise ValueError("all raw four-path runs must pass")
    if [(g["case"], g["variant"]) for g in gates] != [(c, v) for c in CASES for v in VARIANTS]:
        raise ValueError("wrong correctness cases or order")
    if [x["case"] for x in rows] != [c for c in CASES for _ in range(16)]:
        raise ValueError("wrong timing cases or order")
    output, correctness = [], []
    observer_sha = None
    for case in CASES:
        directory, original = root / case, reference / case
        settings, previous = read(directory / "settings.json"), read(original / "settings.json")
        if (any(settings[k] != v for k, v in previous.items() if k not in ("identities", "observer"))
                or any(settings["identities"].get(k) != v for k, v in previous["identities"].items())
                or settings["identities"].keys() != previous["identities"].keys() | {settings["observer"]}):
            raise ValueError("fixed reference inputs changed")
        current_sha = settings["identities"][settings["observer"]]
        if observer_sha is not None and current_sha != observer_sha:
            raise ValueError("observer differs between fixed cases")
        observer_sha = current_sha
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + case + ".json")
        if settings["identities"][settings["replay"]] != sha256(fixture):
            raise ValueError("wrong fixed replay")
        baseline_text = (original / "full-frame/stdout.log").read_text()
        tick = verify_completion(int((original / "full-frame/exit.txt").read_text()), baseline_text)
        expected_observed = [x for x in baseline_text.splitlines() if x.startswith(OBSERVATIONS)]
        reset = records(baseline_text, "HEADLESS_BATTLE_RESET")
        if not expected_observed or len(reset) != 1:
            raise ValueError("complete original observed state and terminal identity required")
        baseline_archive = inspect_archive(original / "full-frame/snapshots.bin", tick)
        validate_terminal_archive(original / "full-frame/snapshots.bin", baseline_archive, reset[0])
        baseline_plan = read(original / "full-frame/plan.json")
        for variant in VARIANTS:
            run = directory / variant
            text = (run / "stdout.log").read_text()
            if verify_completion(int((run / "exit.txt").read_text()), text) != tick:
                raise ValueError("correctness final tick changed")
            validate_markers(text, variant)
            plan, result = read(run / "plan.json"), read(run / "result.json")
            expected_plan = json.loads(json.dumps(baseline_plan))
            replacements = {baseline_plan["output"]: plan["output"],
                            previous["observer"]: settings["observer"]}
            def replace_paths(value):
                for before, after in replacements.items():
                    value = value.replace(before, after)
                return value
            expected_plan["command"] = [replace_paths(x) for x in expected_plan["command"]]
            expected_plan["environment"] = {k: replace_paths(v) for k, v in expected_plan["environment"].items()}
            expected_plan.update(output=plan["output"], identities=settings["identities"],
                                 native_optimization=VARIANTS[variant])
            if variant != "a":
                expected_plan["environment"]["NASHURA_NATIVE_OPTIMIZATION"] = VARIANTS[variant]
            require_equal(plan, expected_plan)
            if Path(plan["output"]).parts[-2:] != (case, variant):
                raise ValueError("wrong correctness output")
            observed = [x for x in text.splitlines() if x.startswith(OBSERVATIONS)]
            if observed != expected_observed:
                raise ValueError("observed state mismatch")
            packed = run / "snapshots.bin.gz"
            with tempfile.TemporaryDirectory(prefix="nashura-fourway-") as temporary:
                unpacked = Path(temporary) / "snapshots.bin"
                with gzip.open(packed, "rb") as source, unpacked.open("xb") as target:
                    shutil.copyfileobj(source, target)
                pair = compare(original / "full-frame/snapshots.bin", unpacked, tick)
            if (not pair["equal"] or pair["right"]["sha256"] != result["archive_sha256"]
                    or pair["right"]["bytes"] != result["archive_bytes"]
                    or pair["records"] != result["records"] or result["final_tick"] != tick):
                raise ValueError("full serializer frame mismatch")
            saved_gate = next(g for g in gates if (g["case"], g["variant"]) == (case, variant))
            require_equal(saved_gate["result"], result)
            require_equal(saved_gate["comparison"], pair)
            if (saved_gate["compressed_snapshot_bytes"] != packed.stat().st_size
                    or saved_gate["compressed_snapshot_sha256"] != sha256(packed)
                    or saved_gate["full_frames_equal"] is not True or saved_gate["observed_equal"] is not True):
                raise ValueError("compressed gate evidence changed")
            correctness.append({"case": case, "variant": variant, "final_tick": tick,
                "frames": pair["records"], "archive_sha256": result["archive_sha256"],
                "observed_lines": len(observed), "full_frames_equal": True, "observed_equal": True})
        selected = [x for x in rows if x["case"] == case]
        verified = []
        for row in selected:
            variant = row["variant"]
            label = str(row["block"]) + "-" + str(row["position"]) + "-" + variant
            run = directory / "timing" / label
            plan, result = read(run / "plan.json"), read(run / "result.json")
            validate_plan(plan, settings, variant, Path(plan["output"]), variants=VARIANTS)
            if Path(plan["output"]).parts[-3:] != (case, "timing", label):
                raise ValueError("wrong original timing output")
            text = (run / "stdout.log").read_text()
            if verify_completion(int((run / "exit.txt").read_text()), text, 116) != tick:
                raise ValueError("timing final tick changed")
            validate_markers(text, variant, timing=True)
            summary = summarize(text, 116, expected_tick=tick, expected_digest=reset[0]["digest"],
                                expected_bytes=int(reset[0]["bytes"]), warmup_matches=16)
            compact = {k: v for k, v in summary.items() if k not in COMPACT_OMIT}
            require_equal(compact, read(directory / "timing" / (label + "-summary.json")))
            require_equal(compact, row["summary"])
            require_equal(result, row["result"])
            if result["matches"] != 116 or result["native_optimization"] != VARIANTS[variant]:
                raise ValueError("wrong timing result")
            environment = read(directory / "timing" / (label + "-environment.json"))
            if environment["cpu_count"] != 2 or environment["affinity"] != [0, 1]:
                raise ValueError("VM CPU allocation changed")
            verified.append({"block": row["block"], "position": row["position"], "variant": variant,
                "summary": compact, "stdout_sha256": sha256(run / "stdout.log"),
                "plan_sha256": sha256(run / "plan.json")})
        aggregate = aggregate_batches(verified)
        aggregate.update(case=case, batches=verified)
        output.append(aggregate)
    return {"schema": "nashura.headless-fourway.v1", "host_recomputed": True,
        "benchmark_batches": 64, "total_matches": 7424, "measured_matches": 6400,
        "orders": list(ORDERS), "observer_sha256": observer_sha,
        "correctness": correctness, "cases": output, "whole_goal_complete": False,
        "limitations": ["four process batches per path per case; no population significance claim",
            "step percentiles are medians of sampled per-match percentiles, not all-step quantiles",
            "terminal checks in timing do not establish every-match all-frame parity",
            "unserialized state, all inputs and long-run stability remain separate boundaries",
            "host frequency, temperature and VM scheduling are not locked"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = summarize_root(args.root, args.reference)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps({k: v for k, v in result.items() if k not in ("cases", "correctness")}, indent=2))


if __name__ == "__main__":
    main()
