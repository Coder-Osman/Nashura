"""Synthetic complete evidence graph and strict rejection, not battle evidence."""
import gzip
import json
import struct
from types import SimpleNamespace

import pytest

import run_headless_match as runner
import summarize_headless_perf as perf
import verify_component_profile as gate
import verify_memory_lifecycle as memory
from test_combined_profile import elf_input
from test_run_headless_match import inputs
from test_summarize_headless_perf import sample
from test_verify_memory_lifecycle import synthetic as memory_evidence


def synthetic(inputs, monkeypatch):
    correctness, reference = memory_evidence(inputs, monkeypatch)
    monkeypatch.setattr(perf, "ENGINE_SHA256", runner.ENGINE_SHA256)
    root = correctness.parent / "component-profile"
    root.mkdir()
    monkeypatch.setattr(gate, "GUEST_ROOT", root)
    monkeypatch.setattr(gate, "REFERENCE", correctness)
    monkeypatch.setattr(gate, "OBSERVER", memory.OBSERVER)
    monkeypatch.setattr(gate, "OBSERVER_SHA256", memory.OBSERVER_SHA256)
    engine = root.parent / "engine.so"
    engine.write_bytes(elf_input(monkeypatch))
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    def env(epoch):
        return dict(cpu_count=2, affinity=[0, 1], memory_mib_expected=6144, epoch_ns=epoch)
    save(root / "preflight.json", env(1))
    save(root / "stage-plan.json", dict(cases=list(gate.CASES), passes=2, repeats=216,
        warmup=16, optimization=gate.VARIANTS["candidate"], observer_sha256=gate.OBSERVER_SHA256,
        concurrency=False, scope="software-cpu-clock-diagnostic-only",
        engineering_checks_finished_before_launch=True))
    order = [(1, c) for c in gate.CASES] + [(2, c) for c in reversed(gate.CASES)]
    save(root / "done.json", dict(runs=8, successes=8))
    save(root / "results.json", [dict(pass_index=p, case=c, perf_exit=0) for p, c in order])
    (root / "wrapper.exit").write_text("0")
    for index, (pass_index, case) in enumerate(order):
        directory = root / ("pass-" + str(pass_index)) / case
        out = directory / "run"
        settings = gate.read(correctness / case / "settings.json")
        commands = len(gate.read(settings["replay"])["cmd"])
        plan = runner.run(settings, out, benchmark_repeats=216, timeout_seconds=120,
            native_optimization=gate.VARIANTS["candidate"], compress_stdout=True)
        save(out / "plan.json", plan)
        command = ["/usr/bin/perf", "record", "--clockid", "mono", "-F", "997", "-e",
            "cpu-clock:u", "-g", "--call-graph", "fp", "-o", str(directory / "perf.data"),
            "--", "/usr/bin/setpriv", "--reuid=1000", "--regid=1000", "--init-groups",
            "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C",
            "/usr/bin/python3", str(gate.TOOLS / "run_headless_match.py"),
            str(correctness / case / "settings.json"), "--output", str(out), "--apply",
            "--benchmark-repeats", "216", "--step-sample-every", "16", "--timeout-seconds",
            "120", "--native-optimization", gate.VARIANTS["candidate"], "--compress-stdout"]
        save(directory / "command.json", command)
        lines = runner.optimization_installation_markers(gate.VARIANTS["candidate"])
        script = []
        for iteration in range(1, 217):
            phase = (iteration - 1) % 16
            start = iteration * 2000
            count = (17 - phase + 15) // 16
            lines.extend([
                f"HEADLESS_BATTLE_METRICS completed=17 wall_ns=1000 step_total_ns=900 step_min_ns=1 step_max_ns=90 p50_ns=5 p95_ns=20 p99_ns=70 peak_rss_kb=2048 trace=0 start_ns={start} end_ns={start+1000} sample_count={count} sample_every=16 sample_phase={phase} sample_phase_period=16 loaded_commands={commands} injected_commands=0",
                "HEADLESS_BATTLE_MEMORY rss_kb=2048 pss_kb=1024",
                "HEADLESS_GAME_STATE_STEP_RETURN tick_before=0 tick_after=17 completed=17 requested=10000 finalized=1",
                f"HEADLESS_BATTLE_RESET iteration={iteration} repeats=216 digest=e16801510db89efd bytes=3 equal=1 validated=1"])
            script.append(sample(f"0.{start:09d}", frames=[(0x100000+104, gate.ENGINE_PATH)]))
        lines.extend(["HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1",
            "HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1", "HEADLESS_REJECTED_PM_CALLS=0",
            "HEADLESS_BATTLE_READY=UNVERIFIED"])
        (out / "stdout.log.gz").write_bytes(gzip.compress(chr(10).join(lines).encode()))
        (out / "exit.txt").write_text("0")
        save(out / "result.json", gate.result_expected("candidate", 17, variants=gate.VARIANTS))
        blobs = dict(script="".join(script), maps=
            f"main 20 0.000000001: PERF_RECORD_MMAP2 20/20: [0x100000(0x2000000) @ 0 00:1e 338 3313453382]: r-xp {gate.ENGINE_PATH}\n",
            evlist="cpu-clock:u: type: 1, use_clockid: 1, clockid: 1\n",
            report="# Total Lost Samples: 0\n")
        for label, text in blobs.items():
            (directory / ("perf-" + label + ".txt.gz")).write_bytes(gzip.compress(text.encode()))
            (directory / ("perf-" + label + ".exit")).write_text("0")
        (directory / "perf-record.exit").write_text("0")
        save(directory / "environment.json", env(index * 100 + 2))
        save(directory / "end-environment.json", env(index * 100 + 99))
    return root, correctness, reference, engine


def test_complete_graph_and_tampered_profiles(inputs, monkeypatch):
    args = synthetic(inputs, monkeypatch)
    root = args[0]
    result = gate.verify(*args)
    assert len(result["cases"]) == 8 and result["reference_full_frames"] == 144
    assert result["whole_goal_complete"] is result["benchmark"] is False
    assert result["cases"][0]["hotspots"]["function_leaf_samples"][0]["samples"] == 200
    case = "pass-1/" + gate.CASES[0]
    for filename, mutate in [
        ("done.json", lambda x: x.update(successes=7)),
        ("results.json", lambda x: x.reverse()),
        ("stage-plan.json", lambda x: x.update(concurrency=True)),
        (case + "/command.json", lambda x: x.append("--native-phases")),
        (case + "/environment.json", lambda x: x.update(epoch_ns=0)),
        (case + "/end-environment.json", lambda x: x.update(cpu_count=4)),
        (case + "/run/plan.json", lambda x: x["environment"].update(NASHURA_PHASE_TIMING="1")),
        (case + "/run/result.json", lambda x: x.update(final_tick=1)),
    ]:
        path = root / filename
        original = path.read_text()
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            gate.verify(*args)
        path.write_text(original)
    for filename, content in [("wrapper.exit", "1"), (case + "/perf-maps.exit", "1"),
        (case + "/run/stdout.log", "unexpected raw log")]:
        path = root / filename
        original = path.read_bytes() if path.exists() else None
        path.write_text(content)
        with pytest.raises(ValueError):
            gate.verify(*args)
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)


def test_actual_leaf_fde_range_is_required(monkeypatch):
    engine = elf_input(monkeypatch)
    from verify_combined_profile import function_regions
    assert function_regions(engine, {"leaf": 119})["leaf"]["end"] == "0x78"
    with pytest.raises(ValueError, match="range disagree"):
        function_regions(engine, {"gap": 120})


@pytest.mark.parametrize("change", [False, True])
def test_unwindless_stubs_require_exact_instruction_shape(monkeypatch, change):
    raw = struct.pack("<4I", 0xb0000830, 0xf941aa11, 0x910d4210,
        0xd61f0220 if not change else 0xd65f03c0)
    monkeypatch.setattr(gate, "Elf64", lambda data: SimpleNamespace(
        address=lambda start, size: 0, slice=lambda start, size: data[:size]))
    if change:
        with pytest.raises(ValueError, match="exact indirect-branch stub"):
            gate.branch_stub(raw, 0x17e4b24)
    else:
        assert gate.branch_stub(raw, 0x17e4b24)["start"] == "0x17e4b20"
        assert gate.branch_stub(raw, 104) is None
