"""Verify four fixed-input combined profiles; report attribution, never speedup."""
import argparse
from bisect import bisect_right
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import struct
import tempfile

from compare_headless_snapshots import inspect_archive
from elf_audit import Elf64
from run_headless_match import ENGINE_SHA256, optimization_installation_markers
from summarize_headless_fourway import CASES, require_equal, sha256
from summarize_headless_perf import (ENGINE_PATH, engine_mappings, file_offset,
                                    sample_records, summarize)
from summarize_headless_threeway import validate_plan, validate_terminal_archive
from summarize_headless_benchmark import records
from verify_combined_followup import VARIANTS, read, validate_actions, validate_result

TARGETS = {"query": 0xf34c60, "grid-clear": 0xf348a8, "object-update": 0xf67024,
           "component-update": 0x115d540, "getter": 0xf20210,
           "pointer-distance": 0x121b860, "scalar-distance": 0x121b8bc}
GUEST_ROOT = Path("/home/gujiga/nashura-combined-profile-20261031-v1")


def validate_command(command, case):
    expected = ["/usr/bin/perf", "record", "--clockid", "mono", "-F", "997", "-e",
        "cpu-clock:u", "-g", "--call-graph", "fp", "-o", str(GUEST_ROOT / case / "perf.data"),
        "--", "/usr/bin/setpriv", "--reuid=1000", "--regid=1000", "--init-groups",
        "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C",
        "/usr/bin/python3", "/tmp/nashura-combined-followup-tools-20261031-v1/run_headless_match.py",
        "/home/gujiga/nashura-combined-followup-20261031-v1/" + case + "/settings.json",
        "--output", str(GUEST_ROOT / case / "run"), "--apply", "--benchmark-repeats", "216",
        "--step-sample-every", "16", "--timeout-seconds", "120",
        "--native-optimization", "hot-distance-v1"]
    require_equal(command, expected)


def function_regions(data):
    if hashlib.sha256(data).hexdigest() != ENGINE_SHA256:
        raise ValueError("exact pinned ELF required")
    elf = Elf64(data)
    headers = [s for s in elf.segments if s["kind"] == 0x6474e550]
    if len(headers) != 1:
        raise ValueError("unique GNU_EH_FRAME required")
    segment = headers[0]
    header = elf.slice(segment["offset"], segment["filesz"])
    if header[:4] != bytes((1, 0x1b, 3, 0x3b)):
        raise ValueError("unexpected fixed unwind table encoding")
    count = struct.unpack_from("<I", header, 8)[0]
    if len(header) != 12 + 8 * count:
        raise ValueError("unwind table size mismatch")
    entries = [(segment["virtual"] + a, segment["virtual"] + b)
               for a, b in struct.iter_unpack("<ii", header[12:])]
    starts = [x[0] for x in entries]
    if starts != sorted(set(starts)):
        raise ValueError("unordered unwind table")
    regions = {}
    for name, target in TARGETS.items():
        index = bisect_right(starts, target) - 1
        if not 0 <= index < len(entries) - 1:
            raise ValueError("target outside unwind table")
        start, fde = entries[index]
        offset = elf.address(fde, 16)
        length = struct.unpack_from("<I", data, offset)[0]
        if not 12 <= length < 1024:
            raise ValueError("unexpected FDE size")
        elf.slice(offset, length + 4)
        pc = fde + 8 + struct.unpack_from("<i", data, offset + 8)[0]
        end = start + struct.unpack_from("<I", data, offset + 12)[0]
        if pc != start or not start <= target < end <= entries[index + 1][0]:
            raise ValueError("FDE and index range disagree")
        raw = elf.slice(elf.address(start, end - start), end - start)
        regions[name] = {"start": hex(start), "end": hex(end),
                         "original_bytes_sha256": hashlib.sha256(raw).hexdigest()}
    return regions


def region_counts(log, script, mapping, regions):
    windows = records(log, "HEADLESS_BATTLE_METRICS")[16:]
    starts = [int(x["start_ns"]) for x in windows]
    maps = engine_mappings(mapping)
    counts = Counter()
    for row in sample_records(script):
        index = bisect_right(starts, row["time_ns"]) - 1
        if (index < 0 or row["time_ns"] >= int(windows[index]["end_ns"])
                or row["comm"] != "Mainloop" or row["frames"][0][1] != ENGINE_PATH):
            continue
        offset = file_offset(row["frames"][0][0], row["pid"], row["time_ns"], maps)
        for name, region in regions.items():
            if int(region["start"], 16) <= offset < int(region["end"], 16):
                counts[name] += 1
    return dict(counts)


def verify(root, correctness, engine):
    root, correctness = map(Path, (root, correctness))
    if (read(root / "done.json") != {"runs": 4, "successes": 4}
            or (root / "wrapper.exit").read_text().strip() != "0"
            or read(root / "results.json") != [{"case": c, "perf_exit": 0} for c in CASES]
            or read(correctness / "correctness-done.json") != {"runs": 8, "successes": 8}):
        raise ValueError("four complete profiles and full-frame reference gates required")
    regions = function_regions(Path(engine).read_bytes())
    results = []
    for name in CASES:
        directory = root / name
        validate_command(read(directory / "command.json"), name)
        settings = read(correctness / name / "settings.json")
        fixture = Path(__file__).with_name("fixtures") / ("headless-runtime-" + name + ".json")
        if sha256(fixture) != settings["identities"][settings["replay"]]:
            raise ValueError("wrong fixed replay")
        baseline = gzip.decompress((correctness / name / "full-original/stdout.log.gz").read_bytes()).decode()
        reset = records(baseline, "HEADLESS_BATTLE_RESET")
        if len(reset) != 1:
            raise ValueError("reference terminal identity required")
        reset = reset[0]
        original = read(correctness / name / "full-original/result.json")
        tick = original["final_tick"]
        with tempfile.TemporaryDirectory(prefix="nashura-profile-reference-") as tmp:
            raw = Path(tmp) / "snapshots.bin"
            raw.write_bytes(gzip.decompress((correctness / name / "full-original/snapshots.bin.gz").read_bytes()))
            archive = inspect_archive(raw, tick)
            if archive["sha256"] != original["archive_sha256"]:
                raise ValueError("reference snapshot identity changed")
            validate_terminal_archive(raw, archive, reset)
        plan = read(directory / "run/plan.json")
        validate_plan(plan, settings, "combined", Path(plan["output"]),
                      variants=VARIANTS, benchmark_repeats=216)
        if Path(plan["output"]) != GUEST_ROOT / name / "run":
            raise ValueError("wrong profile output path")
        text = (directory / "run/stdout.log").read_text()
        markers = [x for x in text.splitlines() if x.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
        if markers != optimization_installation_markers("hot-distance-v1"):
            raise ValueError("all ordered combination installation markers required")
        if any(x.startswith(("HEADLESS_NATIVE_PHASE", "HEADLESS_PHASE_TIMING ",
                "HEADLESS_WARM_RESOURCES ", "HEADLESS_BATTLE_OBSERVATION ",
                "HEADLESS_BATTLE_PLAYER ", "HEADLESS_BATTLE_OBJECT ")) for x in text.splitlines()):
            raise ValueError("diagnostic output mixed into profile workload")
        validate_actions(text, read(fixture))
        validate_result(read(directory / "run/result.json"), "combined", tick, repeats=216)
        for label in ("script", "maps", "evlist", "report"):
            if (directory / ("perf-" + label + ".exit")).read_text().strip() != "0":
                raise ValueError("perf transformation failed")
        script, mapping, evlist, report = [gzip.decompress((directory / ("perf-" + name + ".txt.gz")).read_bytes()).decode()
                                          for name in ("script", "maps", "evlist", "report")]
        exits = [int((directory / "run/exit.txt").read_text()),
                 int((directory / "perf-record.exit").read_text()),
                 int((root / "wrapper.exit").read_text())]
        result = summarize(text, script, mapping, evlist, report, plan, exits,
            repeats=216, warmup_matches=16, expected_tick=tick,
            expected_digest=reset["digest"], expected_bytes=int(reset["bytes"]))
        env = read(directory / "environment.json")
        if env["cpu_count"] != 2 or env["affinity"] != [0, 1]:
            raise ValueError("fixed CPU allocation changed")
        result.update(case=name, region_leaf_samples=region_counts(text, script, mapping, regions),
            source_sha256={str(p.relative_to(directory)): sha256(p) for p in sorted(directory.rglob("*")) if p.is_file()})
        results.append(result)
    return {"schema": "nashura.combined-profile.v1", "host_recomputed": True,
        "cases": results, "regions": regions, "engine_sha256": ENGINE_SHA256,
        "scope": "four fixed inputs, combined candidate, software CPU-clock attribution only",
        "limitations": ["one profile per fixture; sampled shares are not function latency or causal speedup",
            "optimized function addresses refer to patched process instructions, original hashes identify the pinned ELF",
            "fixed unwind encodings and exact ELF only; behavioral labels are not recovered original class names",
            "hardware cycles, instructions and cache events remain unavailable; no off-CPU exclusion claim"],
        "whole_goal_complete": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--correctness", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.correctness, args.engine)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps({"profiles_verified": len(result["cases"]), "whole_goal_complete": False}))


if __name__ == "__main__":
    main()
