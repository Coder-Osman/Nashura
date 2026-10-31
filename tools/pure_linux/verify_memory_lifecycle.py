"""Recompute fixed-input memory diagnostics and their separate full-frame gates."""
import argparse
from copy import deepcopy
import gzip
import json
from pathlib import Path
import tempfile

from compare_headless_snapshots import compare, inspect_archive
from run_headless_match import verify_completion
from summarize_headless_benchmark import records
from summarize_headless_fourway import CASES, OBSERVATIONS, require_equal, sha256
from summarize_headless_memory_lifecycle import summarize
from summarize_headless_threeway import validate_plan, validate_terminal_archive
from verify_combined_followup import validate_actions
from verify_component_sustained import validate_environment
from verify_grid_clear_unroll import read, stdout, result_expected

GUEST_ROOT = Path("/home/gujiga/nashura-memory-lifecycle-20261031-v1")
OBSERVER = "/dev/shm/nashura-headless-memory-lifecycle-20261031-v2.so"
OBSERVER_SHA256 = "58c38e6369db5d336adc90d27d0a90e7f8215644057b4429a2cf9d20d7e9f274"
VARIANTS = {"original": None, "candidate": "hot-distance-component-v1"}
BATCHES, REPEATS, INTERVAL = 16, 516, 128


def verify(root, reference):
    root, reference = Path(root), Path(reference)
    if (root / "controller.exit").read_text().strip() != "0":
        raise ValueError("normal memory diagnostic controller exit required")
    require_equal(read(root / "stage-plan.json"), {"cases": list(CASES),
        "serial_batches": BATCHES, "repeats": REPEATS, "interval": INTERVAL,
        "observer": OBSERVER, "observer_sha256": OBSERVER_SHA256,
        "scope": "diagnostic-only", "formal_timing": False, "concurrency": False,
        "engineering_checks_finished_before_launch": True})
    require_equal(read(root / "done.json"), {"diagnostic_runs": len(CASES),
        "full_runs": len(CASES) * len(VARIANTS), "successes": len(CASES) * (1 + len(VARIANTS))})
    rows, gates = read(root / "diagnostic-results.json"), read(root / "correctness-results.json")
    if ([row["case"] for row in rows] != list(CASES) or
            [(row["case"], row["variant"]) for row in gates] != [(c, v) for c in CASES for v in VARIANTS]):
        raise ValueError("complete ordered memory and correctness graph required")
    initial = read(root / "initial-environment.json")
    validate_environment(initial)
    previous_end, cases, full = initial["epoch_ns"], [], []
    for name, row in zip(CASES, rows):
        case = root / name
        settings, old = read(case / "settings.json"), read(reference / name / "settings.json")
        expected = deepcopy(old)
        expected["observer"] = OBSERVER
        expected["identities"][OBSERVER] = OBSERVER_SHA256
        require_equal(settings, expected)
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + name + ".json")
        if settings["identities"][settings["replay"]] != sha256(fixture):
            raise ValueError("fixed replay identity required")
        replay, prior = read(fixture), reference / name / "full-original"
        baseline = stdout(prior, "original", variants=VARIANTS)
        tick = verify_completion(int((prior / "exit.txt").read_text()), baseline)
        resets = records(baseline, "HEADLESS_BATTLE_RESET")
        observations = [line for line in baseline.splitlines() if line.startswith(OBSERVATIONS)]
        if len(resets) != 1 or not observations:
            raise ValueError("reference terminal and observed state required")
        directory = case / "diagnostic-candidate"
        text = stdout(directory, "candidate", variants=VARIANTS)
        validate_actions(text, replay)
        verify_completion(int((directory / "exit.txt").read_text()), text, BATCHES * REPEATS)
        summary = summarize(text, BATCHES, REPEATS, tick, resets[0]["digest"],
                            int(resets[0]["bytes"]), INTERVAL)
        require_equal(summary, read(case / "memory-summary.json"))
        plan = read(directory / "plan.json")
        if (plan.pop("measurement_scope", None) != "memory-lifecycle-diagnostic-only" or
                plan["environment"].pop("NASHURA_MEMORY_LIFECYCLE_EVERY", None) != str(INTERVAL)):
            raise ValueError("explicit memory-only diagnostic plan required")
        validate_plan(plan, settings, "candidate", GUEST_ROOT / name / directory.name,
            variants=VARIANTS, benchmark_repeats=REPEATS, serial_batches=BATCHES,
            timeout_seconds=900, compress_stdout=True)
        result = read(directory / "result.json")
        expected_result = result_expected("candidate", tick, variants=VARIANTS)
        expected_result.update(matches=BATCHES * REPEATS, serial_batches=BATCHES,
                               measurement_scope="memory-lifecycle-diagnostic-only")
        require_equal(result, expected_result)
        require_equal(row["result"], result)
        start, end = read(case / "diagnostic-environment.json"), read(case / "diagnostic-end-environment.json")
        for env in (start, end):
            validate_environment(env)
        if not previous_end < start["epoch_ns"] < end["epoch_ns"]:
            raise ValueError("strictly serial memory diagnostic windows required")
        previous_end = end["epoch_ns"]
        cases.append({"case": name, "summary": summary,
            "start_epoch_ns": start["epoch_ns"], "end_epoch_ns": end["epoch_ns"]})
        with tempfile.TemporaryDirectory(prefix="nashura-memory-frames-") as temp:
            raw_ref = Path(temp) / "reference.bin"
            raw_ref.write_bytes(gzip.decompress((prior / "snapshots.bin.gz").read_bytes()))
            archive = inspect_archive(raw_ref, tick)
            if archive["sha256"] != read(prior / "result.json")["archive_sha256"]:
                raise ValueError("reference serializer archive changed")
            validate_terminal_archive(raw_ref, archive, resets[0])
            for variant in VARIANTS:
                directory = case / ("full-" + variant)
                text = stdout(directory, variant, variants=VARIANTS)
                validate_actions(text, replay)
                if (any(line.startswith(("HEADLESS_MEMORY_LIFECYCLE", "HEADLESS_MEMORY_MAPPING")) for line in text.splitlines())
                        or verify_completion(int((directory / "exit.txt").read_text()), text) != tick
                        or [line for line in text.splitlines() if line.startswith(OBSERVATIONS)] != observations):
                    raise ValueError("full-frame correctness scope or observed state differs")
                plan, prior_plan = read(directory / "plan.json"), read(prior / "plan.json")
                expected = deepcopy(prior_plan)
                expected["command"] = [value.replace(prior_plan["output"], plan["output"]).replace(old["observer"], OBSERVER) for value in prior_plan["command"]]
                expected["environment"] = {key: value.replace(prior_plan["output"], plan["output"]) for key, value in prior_plan["environment"].items()}
                if VARIANTS[variant]:
                    expected["environment"]["NASHURA_NATIVE_OPTIMIZATION"] = VARIANTS[variant]
                expected.update(output=plan["output"], identities=settings["identities"], native_optimization=VARIANTS[variant])
                require_equal(plan, expected)
                if Path(plan["output"]) != GUEST_ROOT / name / directory.name:
                    raise ValueError("wrong full-frame output path")
                raw = Path(temp) / (variant + ".bin")
                raw.write_bytes(gzip.decompress((directory / "snapshots.bin.gz").read_bytes()))
                pair = compare(raw_ref, raw, tick)
                if not pair["equal"]:
                    raise ValueError("full-frame serializer payload differs")
                result = read(directory / "result.json")
                require_equal(result, result_expected(variant, tick, pair=pair, variants=VARIANTS))
                gate = next(g for g in gates if (g["case"], g["variant"]) == (name, variant))
                require_equal(gate["result"], result)
                if (gate["snapshot_gzip_sha256"] != sha256(directory / "snapshots.bin.gz")
                        or gate["stdout_gzip_sha256"] != sha256(directory / "stdout.log.gz")):
                    raise ValueError("compressed full-frame evidence changed")
                full.append(dict(case=name, variant=variant, frames=pair["records"],
                    archive_sha256=pair["right"]["sha256"], observed_lines=len(observations)))
    return dict(schema="nashura.memory-lifecycle-stage.v1", host_recomputed=True,
        observer_sha256=OBSERVER_SHA256, scope="diagnostic-only", benchmark=False,
        whole_goal_complete=False, cases=cases, full=full,
        total_matches=len(CASES) * BATCHES * REPEATS,
        limitations=["allocator counters and mapping names do not identify individual leaks",
            "no speedup inference from separately instrumented diagnostic processes",
            "bounded fixed-input processes; no indefinite stability claim"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.reference)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({key: value for key, value in result.items() if key not in ("cases", "full")}, indent=2))


if __name__ == "__main__":
    main()
