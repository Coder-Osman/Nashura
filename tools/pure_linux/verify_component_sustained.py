"""Recompute bounded component-combination stability and post-run frame gates."""
import argparse
import gzip
import json
from pathlib import Path
import re
import tempfile

from compare_headless_snapshots import compare, inspect_archive
from run_headless_match import verify_completion
from summarize_headless_benchmark import records
from summarize_headless_fourway import CASES, OBSERVATIONS, require_equal, sha256
from summarize_headless_sustained import summarize
from summarize_headless_threeway import validate_plan, validate_terminal_archive
from verify_combined_followup import validate_actions
from verify_component_guard import OBSERVER, OBSERVER_SHA256
from verify_grid_clear_unroll import read, stdout, result_expected

GUEST_ROOT = Path("/home/gujiga/nashura-component-sustained-20261031-v1")
VARIANTS = {"original": None, "candidate": "hot-distance-component-v1"}
BATCHES, REPEATS, WARMUP = 64, 516, 16
HARNESS_SHA256 = "93c324d722b49de87c340a3055745e7c3e652e48e262dcf7915feb809582667e"


def validate_instruction(check, timing):
    matches = [re.fullmatch(r"COMPONENT_GUARD_CHECK cases=120000 original_and_candidate=equal "
        r"guard_pages=passed callee_saved=passed side_effect_fallback=passed "
        r"matched=4593 fallback=18433 null=4617 rejected=92357", line)
        for line in check["stdout"].splitlines()]
    if (check["exit_code"] != 0 or check["stderr"] or check.get(timing) is not True
            or check["harness_sha256"] != HARNESS_SHA256 or sum(x is not None for x in matches) != 1):
        raise ValueError("unchanged real ARM64 ABI/side-effect check required")


def validate_environment(env):
    if (env["cpu_count"] != 2 or env["affinity"] != [0, 1]
            or env["memory_mib_expected"] != 6144 or env["epoch_ns"] <= 0):
        raise ValueError("fixed VM environment required")


def verify(root, reference, *, variants=None, guest_root=None, observer=None,
           observer_sha256=None, instruction_validator=None, schema=
           "nashura.component-sustained.v1"):
    variants = VARIANTS if variants is None else variants
    guest_root = GUEST_ROOT if guest_root is None else Path(guest_root)
    observer = OBSERVER if observer is None else observer
    observer_sha256 = OBSERVER_SHA256 if observer_sha256 is None else observer_sha256
    instruction_validator = validate_instruction if instruction_validator is None else instruction_validator
    root, reference = Path(root), Path(reference)
    if (root / "controller.exit").read_text().strip() != "0":
        raise ValueError("normal controller exit required")
    stage, done = read(root / "stage-plan.json"), read(root / "done.json")
    require_equal(stage, {"cases": list(CASES), "serial_batches": BATCHES,
        "benchmark_repeats": REPEATS, "warmup_matches": WARMUP,
        "optimization": variants["candidate"], "observer_sha256": observer_sha256,
        "profiling": False, "concurrency": False, "engineering_checks_finished_before_launch": True})
    require_equal(done, {"sustained_runs": len(CASES), "post_full_runs": len(CASES) * len(variants),
                         "successes": len(CASES) * (1 + len(variants))})
    for file, when in (("instruction-precheck.json", "before_formal_timing"),
                       ("instruction-check.json", "after_formal_timing")):
        instruction_validator(read(root / file), when)
    rows, gates = read(root / "sustained-results.json"), read(root / "correctness-results.json")
    if ([x["case"] for x in rows] != list(CASES)
            or [(x["case"], x["variant"]) for x in gates] != [(c, v) for c in CASES for v in variants]):
        raise ValueError("complete ordered sustained and post-full graph required")
    results, correctness, previous_end = [], [], 0
    for name, row in zip(CASES, rows):
        case = root / name
        settings, prior_settings = read(case / "settings.json"), read(reference / name / "settings.json")
        expected = json.loads(json.dumps(prior_settings))
        expected["observer"] = observer
        expected["identities"][observer] = observer_sha256
        require_equal(settings, expected)
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + name + ".json")
        if settings["identities"][settings["replay"]] != sha256(fixture):
            raise ValueError("fixed replay identity required")
        replay = read(fixture)
        original = reference / name / "full-original"
        baseline = stdout(original, "original", variants=variants)
        tick = verify_completion(int((original / "exit.txt").read_text()), baseline)
        reset = records(baseline, "HEADLESS_BATTLE_RESET")
        observations = [line for line in baseline.splitlines() if line.startswith(OBSERVATIONS)]
        if len(reset) != 1 or not observations:
            raise ValueError("reference terminal and observed states required")
        directory = case / "sustained-candidate"
        text = stdout(directory, "candidate", timing=True, variants=variants)
        validate_actions(text, replay)
        if verify_completion(int((directory / "exit.txt").read_text()), text, BATCHES * REPEATS) != tick:
            raise ValueError("sustained final tick differs")
        plan = read(directory / "plan.json")
        validate_plan(plan, settings, "candidate", guest_root / name / directory.name,
            variants=variants, benchmark_repeats=REPEATS, serial_batches=BATCHES,
            timeout_seconds=1800, compress_stdout=True)
        result = read(directory / "result.json")
        expected_result = result_expected("candidate", tick, variants=variants)
        expected_result.update(matches=BATCHES * REPEATS, serial_batches=BATCHES)
        require_equal(result, expected_result)
        require_equal(row["result"], result)
        summary = summarize(text, BATCHES, REPEATS, tick, reset[0]["digest"], int(reset[0]["bytes"]), WARMUP)
        require_equal(summary, read(case / "sustained-summary.json"))
        env, end = read(case / "sustained-environment.json"), read(case / "sustained-end-environment.json")
        validate_environment(env)
        validate_environment(end)
        if not previous_end < env["epoch_ns"] < end["epoch_ns"]:
            raise ValueError("strictly serial process windows required")
        previous_end = end["epoch_ns"]
        results.append({"case": name, "summary": summary, "start_epoch_ns": env["epoch_ns"],
            "end_epoch_ns": end["epoch_ns"], "plan_sha256": sha256(directory / "plan.json")})
        with tempfile.TemporaryDirectory(prefix="nashura-sustained-frames-") as temporary:
            raw_ref = Path(temporary) / "reference.bin"
            raw_ref.write_bytes(gzip.decompress((original / "snapshots.bin.gz").read_bytes()))
            archive = inspect_archive(raw_ref, tick)
            if archive["sha256"] != read(original / "result.json")["archive_sha256"]:
                raise ValueError("reference serializer archive changed")
            validate_terminal_archive(raw_ref, archive, reset[0])
            for variant in variants:
                directory = case / ("post-full-" + variant)
                text = stdout(directory, variant, variants=variants)
                validate_actions(text, replay)
                if (verify_completion(int((directory / "exit.txt").read_text()), text) != tick
                        or [x for x in text.splitlines() if x.startswith(OBSERVATIONS)] != observations):
                    raise ValueError("post-full observed state differs")
                prior, plan = read(original / "plan.json"), read(directory / "plan.json")
                expected_plan = json.loads(json.dumps(prior))
                expected_plan["command"] = [x.replace(prior["output"], plan["output"]).replace(prior_settings["observer"], observer) for x in prior["command"]]
                expected_plan["environment"] = {k: v.replace(prior["output"], plan["output"]) for k, v in prior["environment"].items()}
                if variants[variant]:
                    expected_plan["environment"]["NASHURA_NATIVE_OPTIMIZATION"] = variants[variant]
                expected_plan.update(output=plan["output"], identities=settings["identities"], native_optimization=variants[variant])
                require_equal(plan, expected_plan)
                if Path(plan["output"]) != guest_root / name / directory.name:
                    raise ValueError("wrong post-full output path")
                raw = Path(temporary) / (variant + ".bin")
                raw.write_bytes(gzip.decompress((directory / "snapshots.bin.gz").read_bytes()))
                pair = compare(raw_ref, raw, tick)
                if not pair["equal"]:
                    raise ValueError("post-full serializer payload differs")
                result = read(directory / "result.json")
                require_equal(result, result_expected(variant, tick, pair=pair, variants=variants))
                gate = next(x for x in gates if (x["case"], x["variant"]) == (name, variant))
                require_equal(gate["result"], result)
                if (gate["snapshot_gzip_sha256"] != sha256(directory / "snapshots.bin.gz")
                        or gate["stdout_gzip_sha256"] != sha256(directory / "stdout.log.gz")):
                    raise ValueError("post-full compressed evidence changed")
                correctness.append({"case": name, "variant": variant, "frames": pair["records"],
                    "archive_sha256": pair["right"]["sha256"], "observed_lines": len(observations)})
    first_post = read(root / "post-full-environment.json")
    validate_environment(first_post)
    if first_post["epoch_ns"] <= previous_end:
        raise ValueError("post-full diagnostics must follow all sustained timing")
    return {"schema": schema, "host_recomputed": True,
        "observer_sha256": observer_sha256, "cases": results, "post_full": correctness,
        "total_matches": len(CASES) * BATCHES * REPEATS,
        "measured_matches": len(CASES) * BATCHES * (REPEATS - WARMUP),
        "whole_goal_complete": False,
        "limitations": ["bounded fixed-input serial processes; not a 24-hour or leak-free claim",
            "terminal checks during timing; post-full correctness is a separate new process",
            "no causal speedup comparison against earlier sessions or global significance claim",
            "no hardware cycles/cache or complete internal-state equivalence conclusion"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.root, args.reference)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({k: v for k, v in result.items() if k not in ("cases", "post_full")}))


if __name__ == "__main__":
    main()
