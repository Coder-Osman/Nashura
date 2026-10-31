"""Recompute two serial profile passes on the unchanged component combination."""
import argparse
from bisect import bisect_right
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import struct

from summarize_headless_benchmark import records
from summarize_headless_fourway import CASES, require_equal, sha256
from summarize_headless_perf import ENGINE_PATH, engine_mappings, file_offset, sample_records, summarize
from summarize_headless_threeway import validate_plan
from verify_combined_followup import validate_actions
from verify_combined_profile import function_regions, region_counts
from verify_component_sustained import validate_environment
from verify_grid_clear_unroll import read, stdout, result_expected
from verify_memory_lifecycle import verify as verify_reference, OBSERVER, OBSERVER_SHA256
from elf_audit import Elf64

GUEST_ROOT = Path("/home/gujiga/nashura-component-profile-20261031-v1")
REFERENCE = Path("/home/gujiga/nashura-memory-lifecycle-20261031-v1")
TOOLS = Path("/dev/shm/nashura-memory-lifecycle-tools-20261031-v1")
VARIANTS = {"candidate": "hot-distance-component-v1"}
QUERY_BLOCKS = {"setup": (0xf34b3c, 0xf34c38), "cell_loop": (0xf34c38, 0xf34ca4),
    "radius_filter": (0xf34ca4, 0xf34dac), "point_filter": (0xf34dac, 0xf34e90),
    "return_and_append": (0xf34e90, 0xf35064)}


def branch_stub(engine, offset):
    # Pinned ELF's 16-byte ADRP/LDR/ADD/BR import veneers have no FDE.
    # Keep them separate from unwind-proven function bodies and do not invent
    # an imported function name from the nearest symbol.
    start = offset & ~15
    if not 0x17e4af0 <= start < 0x17e6550:
        return None
    elf = Elf64(engine)
    raw = elf.slice(elf.address(start, 16), 16)
    adrp, ldr, add, branch = struct.unpack("<4I", raw)
    if (adrp & 0x9f00001f != 0x90000010 or ldr & 0xffc003ff != 0xf9400211
            or add & 0xffc003ff != 0x91000210 or branch != 0xd61f0220
            or ((ldr >> 10) & 4095) * 8 != ((add >> 10) & 4095)):
        raise ValueError("uncovered address is not an exact indirect-branch stub")
    return dict(start=hex(start), end=hex(start + 16),
        original_bytes_sha256=hashlib.sha256(raw).hexdigest())


def validate_command(command, pass_index, case):
    out = GUEST_ROOT / ("pass-" + str(pass_index)) / case
    expected = ["/usr/bin/perf", "record", "--clockid", "mono", "-F", "997", "-e",
        "cpu-clock:u", "-g", "--call-graph", "fp", "-o", str(out / "perf.data"),
        "--", "/usr/bin/setpriv", "--reuid=1000", "--regid=1000", "--init-groups",
        "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C",
        "/usr/bin/python3", str(TOOLS / "run_headless_match.py"),
        str(REFERENCE / case / "settings.json"), "--output", str(out / "run"), "--apply",
        "--benchmark-repeats", "216", "--step-sample-every", "16",
        "--timeout-seconds", "120", "--native-optimization", VARIANTS["candidate"],
        "--compress-stdout"]
    require_equal(command, expected)


def hotspots(text, script, mapping, engine):
    windows = records(text, "HEADLESS_BATTLE_METRICS")[16:]
    starts = [int(row["start_ns"]) for row in windows]
    mappings, leaves = engine_mappings(mapping), Counter()
    for row in sample_records(script):
        index = bisect_right(starts, row["time_ns"]) - 1
        if (index < 0 or row["time_ns"] >= int(windows[index]["end_ns"])
                or row["comm"] != "Mainloop" or row["frames"][0][1] != ENGINE_PATH):
            continue
        leaves[file_offset(row["frames"][0][0], row["pid"], row["time_ns"], mappings)] += 1
    # Resolve every observed leaf via both unwind index and FDE, instead of
    # assigning class names from nearest arbitrary symbols.
    stubs = {offset: branch_stub(engine, offset) for offset in leaves}
    regions = function_regions(engine, {hex(offset): offset for offset in leaves if stubs[offset] is None})
    by_function, by_stub = Counter(), Counter()
    for offset, count in leaves.items():
        region = stubs[offset] or regions[hex(offset)]
        counter = by_stub if stubs[offset] else by_function
        counter[(region["start"], region["end"], region["original_bytes_sha256"])] += count
    return dict(function_leaf_samples=[dict(start=start, end=end, original_bytes_sha256=digest, samples=count)
        for (start, end, digest), count in sorted(by_function.items(), key=lambda item: (-item[1], item[0]))],
        branch_stub_leaf_samples=[dict(start=start, end=end, original_bytes_sha256=digest, samples=count)
        for (start, end, digest), count in sorted(by_stub.items(), key=lambda item: (-item[1], item[0]))],
        accounted_libg_leaf_samples=sum(by_function.values()) + sum(by_stub.values()),
        query_block_leaf_samples={name: sum(count for offset, count in leaves.items() if start <= offset < end)
                                 for name, (start, end) in QUERY_BLOCKS.items()})


def verify(root, correctness, original_reference, engine):
    root, correctness = Path(root), Path(correctness)
    reference_gate = verify_reference(correctness, original_reference)
    require_equal(read(root / "stage-plan.json"), dict(cases=list(CASES), passes=2, repeats=216,
        warmup=16, optimization=VARIANTS["candidate"], observer_sha256=OBSERVER_SHA256,
        concurrency=False, scope="software-cpu-clock-diagnostic-only",
        engineering_checks_finished_before_launch=True))
    order = [(1, case) for case in CASES] + [(2, case) for case in reversed(CASES)]
    if ((root / "wrapper.exit").read_text().strip() != "0" or
            read(root / "done.json") != dict(runs=8, successes=8) or
            read(root / "results.json") != [dict(pass_index=p, case=c, perf_exit=0) for p, c in order]):
        raise ValueError("two complete forward/reverse profile passes required")
    preflight = read(root / "preflight.json")
    validate_environment(preflight)
    previous_end, results = preflight["epoch_ns"], []
    data = Path(engine).read_bytes()
    regions = function_regions(data)
    for pass_index, name in order:
        directory = root / ("pass-" + str(pass_index)) / name
        validate_command(read(directory / "command.json"), pass_index, name)
        settings = read(correctness / name / "settings.json")
        if settings["observer"] != OBSERVER or settings["identities"][OBSERVER] != OBSERVER_SHA256:
            raise ValueError("unchanged correctness observer required")
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + name + ".json")
        if sha256(fixture) != settings["identities"][settings["replay"]]:
            raise ValueError("fixed replay identity required")
        baseline = gzip.decompress((correctness / name / "full-original/stdout.log.gz").read_bytes()).decode()
        reset = records(baseline, "HEADLESS_BATTLE_RESET")
        if len(reset) != 1:
            raise ValueError("unique reference terminal identity required")
        tick = read(correctness / name / "full-original/result.json")["final_tick"]
        plan = read(directory / "run/plan.json")
        validate_plan(plan, settings, "candidate", GUEST_ROOT / directory.relative_to(root) / "run",
            variants=VARIANTS, benchmark_repeats=216, compress_stdout=True)
        text = stdout(directory / "run", "candidate", timing=True, variants=VARIANTS)
        validate_actions(text, read(fixture))
        require_equal(read(directory / "run/result.json"), result_expected("candidate", tick, variants=VARIANTS))
        for label in ("script", "maps", "evlist", "report"):
            if (directory / ("perf-" + label + ".exit")).read_text().strip() != "0":
                raise ValueError("perf transformation failed")
        script, mapping, evlist, report = [gzip.decompress((directory / ("perf-" + label + ".txt.gz")).read_bytes()).decode()
            for label in ("script", "maps", "evlist", "report")]
        result = summarize(text, script, mapping, evlist, report, plan,
            [int((directory / "run/exit.txt").read_text()), int((directory / "perf-record.exit").read_text()),
             int((root / "wrapper.exit").read_text())], repeats=216, warmup_matches=16,
            expected_tick=tick, expected_digest=reset[0]["digest"], expected_bytes=int(reset[0]["bytes"]))
        begin, end = read(directory / "environment.json"), read(directory / "end-environment.json")
        for value in (begin, end):
            validate_environment(value)
        if not previous_end < begin["epoch_ns"] < end["epoch_ns"]:
            raise ValueError("strictly serial profile process windows required")
        previous_end = end["epoch_ns"]
        resolved = hotspots(text, script, mapping, data)
        if (resolved["accounted_libg_leaf_samples"] != result["mainloop_libg_leaf_samples"]
                or sum(resolved["query_block_leaf_samples"].values()) != result["spatial_query_leaf_samples"]):
            raise ValueError("all libg/query leaf samples must reconcile")
        result.update(case=name, pass_index=pass_index,
            region_leaf_samples=region_counts(text, script, mapping, regions),
            hotspots=resolved,
            source_sha256={str(p.relative_to(directory)): sha256(p) for p in sorted(directory.rglob("*")) if p.is_file()})
        results.append(result)
    return dict(schema="nashura.component-profile.v1", host_recomputed=True,
        scope="two serial software CPU-clock profile passes; no speedup comparison",
        benchmark=False, whole_goal_complete=False, observer_sha256=OBSERVER_SHA256,
        reference_full_frames=sum(row["frames"] for row in reference_gate["full"]),
        regions=regions, cases=results,
        limitations=["sample shares and IP skid do not measure function latency, cycles or cache misses",
            "patched libg and anonymous islands require their installed-code interpretation",
            "return PCs and leaf IPs are distinct; no off-CPU exclusion claim",
            "two profiles per fixed fixture; no inference to all battle inputs"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "correctness", "original_reference", "engine", "output"):
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.correctness, args.original_reference, args.engine)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps(dict(profiles=len(result["cases"]), whole_goal_complete=False)))


if __name__ == "__main__":
    main()
