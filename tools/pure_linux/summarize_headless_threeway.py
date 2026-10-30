"""Recompute balanced serial original/hot/component batches from raw evidence.

Requires all six triple permutations per fixed case and independently verifies
plans, exits, native completion, optimization markers and benchmark summaries.
Batch medians are the comparison units; matches within a batch are not IID.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from run_headless_match import BOOT, ENGINE_SHA256, OPTIMIZATION_MARKERS, verify_completion
from summarize_headless_benchmark import records, summarize
from compare_headless_snapshots import inspect_archive

ORDERS = ("abc", "bca", "cab", "cba", "acb", "bac")
VARIANTS = {"a": None, "b": "hot-getters-v1", "c": "component-getters-v1"}
FORBIDDEN_ENV = {"NASHURA_NATIVE_PHASES", "NASHURA_PHASE_TIMING", "NASHURA_TRACE_SNAPSHOTS",
                 "NASHURA_BATTLE_OBSERVATIONS", "NASHURA_DEPLOY_HAND"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_terminal_archive(path, archive, reset):
    size = archive["frames"][-1]["bytes"]
    with Path(path).open("rb") as source:
        source.seek(-size, 2)
        payload = source.read()
    value = 1469598103934665603  # Exact pinned snapshot_digest offset, not standard FNV offset.
    for byte in payload:
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    if reset["digest"] != format(value, "016x") or int(reset["bytes"]) != size:
        raise ValueError("correctness terminal identity differs from archived serializer")


def validate_plan(plan, settings, variant, directory, *, variants=None,
                  benchmark_repeats=116, serial_batches=1, timeout_seconds=120,
                  compress_stdout=False):
    variants = VARIANTS if variants is None else variants
    env = plan["environment"]
    if (plan["native_optimization"] != variants[variant] or plan["native_phases"]
            or plan["snapshot_trace"] or plan["record_hand_actions"]
            or plan["benchmark_repeats"] != benchmark_repeats or plan["step_sample_every"] != 16
            or plan.get("thread_resources", False) or plan.get("serial_batches", 1) != serial_batches
            or plan.get("stdout_encoding") != ("gzip-after-process-exit" if compress_stdout else None)
            or FORBIDDEN_ENV.intersection(env)):
        raise ValueError("diagnostic or wrong timing plan")
    expected_env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C",
        "CLASSPATH": "/oldroot" + settings["probe_dex"] + ":/data/local/tmp/current-game-classes.dex",
        "NASHURA_BATTLE_STEPS": "10000", "NASHURA_SNAPSHOT_ADAPTER": "1",
        "NASHURA_QUIET_SNAPSHOTS": "1", "NASHURA_BENCHMARK_VALIDATE": "1",
        "NASHURA_QUIET": "1", "NASHURA_BENCHMARK_MINIMAL": "1",
        "NASHURA_RESET_REPEATS": str(benchmark_repeats), "NASHURA_STEP_SAMPLE_EVERY": "16",
        "NASHURA_MEMORY_SAMPLE": "1", "NASHURA_TASK_TIMEOUT_MS": str(min(timeout_seconds * 1000, 120000))}
    if serial_batches > 1:
        expected_env["NASHURA_SERIAL_BATCHES"] = str(serial_batches)
    if variants[variant] is not None:
        expected_env["NASHURA_NATIVE_OPTIMIZATION"] = variants[variant]
    if env != expected_env:
        raise ValueError("wrong benchmark environment")
    closure = Path(settings["closure"])
    expected_command = ["timeout", "-k", "3s", str(timeout_seconds) + "s", "unshare", "-Ur", "-m", "-n", "-i",
        "/bin/sh", settings["namespace"], str(closure), settings["linker_config"],
        "/" + str(Path(settings["runtime"]).relative_to(closure)),
        "-Xbootclasspath:" + ":".join(BOOT), "-Djava.library.path=/data/local/tmp/libg-input",
        "--probe-args", "/data/local/tmp/libg-input/libg.so", "/data/local/tmp/libart_engine_jni_probe.so",
        "/oldroot" + settings["apk"], "/oldroot" + str(directory) + "/probe-data",
        "callbacks", "apk-context", "/oldroot" + settings["observer"],
        "native-create", "native-destroy", "native-active", "game-state-action-step",
        "/oldroot" + settings["replay"], settings["identities"][settings["replay"]]]
    if plan["command"] != expected_command or plan["output"] != str(directory):
        raise ValueError("wrong benchmark command or output")
    if plan["identities"] != settings["identities"]:
        raise ValueError("benchmark inputs differ from correctness inputs")
    engine = str(closure / "data/local/tmp/libg-input/libg.so")
    if plan["identities"].get(engine) != ENGINE_SHA256:
        raise ValueError("missing fixed engine identity")


def aggregate_batches(rows):
    if len(rows) != 18:
        raise ValueError("complete six-block three-way case required")
    by_key = {(row["block"], row["variant"]): row for row in rows}
    if set(by_key) != {(block, variant) for block in range(1, 7) for variant in VARIANTS}:
        raise ValueError("duplicate or invalid three-way batch")
    variants = {}
    for name in VARIANTS:
        batches = [by_key[block, name] for block in range(1, 7)]
        total_ticks = sum(row["summary"]["measured_matches"] * row["summary"]["tick"] for row in batches)
        seconds = sum(row["summary"]["timed_seconds"] for row in batches)
        variants[name] = {
            "optimization": VARIANTS[name], "batches": 6,
            "matches": sum(row["summary"]["matches"] for row in batches),
            "measured_matches": sum(row["summary"]["measured_matches"] for row in batches),
            "batch_median_ms": [row["summary"]["median_match_ms"] for row in batches],
            "median_of_batch_medians_ms": statistics.median(row["summary"]["median_match_ms"] for row in batches),
            "aggregate_ticks_per_second": total_ticks / seconds,
            "step_percentiles_us": {key: statistics.median(row["summary"]["step_percentiles_us"][key] for row in batches)
                                    for key in ("p50", "p95", "p99")},
            "sampled_rss_peak_mib": max((row["summary"]["sampled_rss_peak_mib"] for row in batches
                                        if row["summary"]["sampled_rss_peak_mib"] is not None), default=None),
        }
    comparisons = {}
    for before, after in (("a", "b"), ("a", "c"), ("b", "c")):
        comparisons[before + "_to_" + after] = {
            "median_duration_change_percent": (variants[after]["median_of_batch_medians_ms"] /
                                                variants[before]["median_of_batch_medians_ms"] - 1) * 100,
            "aggregate_throughput_change_percent": (variants[after]["aggregate_ticks_per_second"] /
                                                     variants[before]["aggregate_ticks_per_second"] - 1) * 100,
            "paired_block_duration_change_percent": [
                (by_key[block, after]["summary"]["median_match_ms"] /
                 by_key[block, before]["summary"]["median_match_ms"] - 1) * 100
                for block in range(1, 7)],
            "faster_blocks": sum(by_key[block, after]["summary"]["median_match_ms"] <
                                 by_key[block, before]["summary"]["median_match_ms"] for block in range(1, 7)),
        }
    return {"variants": variants, "comparisons": comparisons}


def validate_saved_summary(result, saved, compact):
    expected = {key: value for key, value in result.items() if key not in
        ("samples", "memory_samples", "step_sample_phase_by_match", "step_sample_phase_period_by_match")}
    if result != saved or compact != expected:
        raise ValueError("saved summary differs from raw log")


def summarize_root(root, correctness_root):
    root = Path(root)
    correctness_root = Path(correctness_root)
    if json.loads((correctness_root / "done.json").read_text()) != {"cases": 4, "successes": 4}:
        raise ValueError("complete full-frame correctness gate required")
    done = json.loads((root / "benchmark-done.json").read_text())
    rows = json.loads((root / "benchmark-results.json").read_text())
    if done != {"batches": 72, "successes": 72} or len(rows) != 72 or any("error" in row for row in rows):
        raise ValueError("all 72 serial batches must pass")
    expected_cases = ["deck-" + deck + "-seed-" + seed for deck in ("a", "b") for seed in ("1", "7")]
    if sorted({row["case"] for row in rows}) != expected_cases:
        raise ValueError("wrong fixed cases")
    cases = []
    for case in expected_cases:
        correct = correctness_root / case / "original"
        correct_text = (correct / "stdout.log").read_text()
        correct_tick = verify_completion(int((correct / "exit.txt").read_text()), correct_text)
        correct_reset = records(correct_text, "HEADLESS_BATTLE_RESET")
        if len(correct_reset) != 1:
            raise ValueError("unique correctness terminal identity required")
        archive = inspect_archive(correct / "snapshots.bin", correct_tick)
        correct_result = json.loads((correct / "result.json").read_text())
        if archive["sha256"] != correct_result["archive_sha256"]:
            raise ValueError("correctness archive changed")
        validate_terminal_archive(correct / "snapshots.bin", archive, correct_reset[0])
        settings = json.loads((correctness_root / case / "settings.json").read_text())
        replay_sha = sha256(Path(__file__).with_name("fixtures") / ("headless-runtime-" + case + ".json"))
        if settings["identities"][settings["replay"]] != replay_sha:
            raise ValueError("wrong published replay identity")
        selected = [row for row in rows if row["case"] == case]
        expected = [(block, position, variant) for block, order in enumerate(ORDERS, 1)
                    for position, variant in enumerate(order, 1)]
        if [(row["block"], row["position"], row["variant"]) for row in selected] != expected:
            raise ValueError("unbalanced serial batch order")
        reference = None
        for row in selected:
            block, position, variant = row["block"], row["position"], row["variant"]
            label = str(block) + "-" + str(position) + "-" + variant
            if row["label"] != label:
                raise ValueError("wrong batch label")
            directory = root / case / label
            plan = json.loads((directory / "plan.json").read_text())
            # Archived directories have moved to the host; validate the original
            # guest output path rather than substituting the recovered path.
            validate_plan(plan, settings, variant, Path(plan["output"]))
            if Path(plan["output"]).parts[-2:] != (case, label):
                raise ValueError("wrong original batch directory")
            identities = plan["identities"]
            if reference is None:
                reference = identities
            elif identities != reference:
                raise ValueError("different inputs within a case")
            raw = (directory / "stdout.log").read_bytes()
            text = raw.decode()
            tick = verify_completion(int((directory / "exit.txt").read_text()), text, 116)
            if tick != correct_tick:
                raise ValueError("benchmark final tick differs from correctness")
            markers = [line for line in text.splitlines() if line.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
            expected_marker = [] if variant == "a" else ["HEADLESS_NATIVE_OPTIMIZATION name=" + VARIANTS[variant]
                                                       + " installed=1 " + OPTIMIZATION_MARKERS[VARIANTS[variant]]]
            if markers != expected_marker or any(line.startswith(("HEADLESS_NATIVE_PHASE", "HEADLESS_PHASE_TIMING ",
                    "HEADLESS_BATTLE_OBSERVATION ", "HEADLESS_BATTLE_PLAYER ", "HEADLESS_BATTLE_OBJECT "))
                    for line in text.splitlines()):
                raise ValueError("diagnostic output or missing candidate install")
            result = summarize(text, 116, expected_tick=correct_tick, expected_digest=correct_reset[0]["digest"],
                               expected_bytes=int(correct_reset[0]["bytes"]), warmup_matches=16)
            result["log_sha256"] = hashlib.sha256(raw).hexdigest()
            saved = json.loads((root / case / (label + "-summary.json")).read_text())
            validate_saved_summary(result, saved, row["summary"])
            row["summary"] = result
            row["stdout_sha256"] = result["log_sha256"]
            environment = json.loads((root / case / (label + "-environment.json")).read_text())
            if environment["cpu_count"] != 2 or environment["affinity"] != [0, 1]:
                raise ValueError("VM CPU allocation changed")
        case_result = aggregate_batches(selected)
        case_result.update(case=case, input_identities=reference,
                           batches=[{key: row[key] for key in ("block", "position", "variant", "label", "stdout_sha256")}
                                    for row in selected])
        cases.append(case_result)
    return {"schema": "nashura.headless-threeway.v1", "benchmark_batches": 72,
            "total_matches": 8352, "measured_matches": 7200, "cases": cases,
            "orders": list(ORDERS), "whole_goal_complete": False,
            "method": "median of six batch medians; aggregate ticks/time; six paired triple blocks",
            "limitations": ["batch is the comparison unit; within-batch matches are not IID",
                            "six blocks per case; no population significance claim",
                            "host frequency, temperature and VM scheduling are not locked",
                            "benchmark checks terminal digest; full frame parity is a separate gate"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--correctness", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize_root(args.root, args.correctness)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
