"""Synthetic graph tests exercise rejection; real evidence remains mandatory."""
from copy import deepcopy
import gzip
import json
import shutil

import pytest
import run_headless_match as runner
import verify_component_sustained as gate
from test_combined_followup import combined_evidence
from test_run_headless_match import inputs


def synthetic(inputs, monkeypatch):
    reference, _, _ = combined_evidence(inputs, monkeypatch)
    root = reference.parent / "sustained"
    root.mkdir()
    observer = root.parent / "sustained-observer"
    observer.write_bytes(b"synthetic observer")
    monkeypatch.setattr(gate, "GUEST_ROOT", root)
    monkeypatch.setattr(gate, "OBSERVER", str(observer))
    monkeypatch.setattr(gate, "OBSERVER_SHA256", gate.sha256(observer))
    monkeypatch.setattr(gate, "BATCHES", 6)
    monkeypatch.setattr(gate, "REPEATS", 18)
    monkeypatch.setattr(gate, "WARMUP", 2)
    def save(p, value):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))
    def env(epoch):
        return dict(cpu_count=2, affinity=[0, 1], memory_mib_expected=6144, epoch_ns=epoch)
    save(root / "stage-plan.json", dict(cases=list(gate.CASES), serial_batches=6,
        benchmark_repeats=18, warmup_matches=2, optimization=gate.VARIANTS["candidate"],
        observer_sha256=gate.OBSERVER_SHA256, profiling=False, concurrency=False,
        engineering_checks_finished_before_launch=True))
    save(root / "done.json", dict(sustained_runs=4, post_full_runs=8, successes=12))
    save(root / "post-full-environment.json", env(1000))
    (root / "controller.exit").write_text("0")
    for filename, when in (("instruction-precheck.json", "before_formal_timing"),
                           ("instruction-check.json", "after_formal_timing")):
        save(root / filename, {"exit_code": 0, "stderr": "", when: True,
            "harness_sha256": gate.HARNESS_SHA256, "stdout":
            "COMPONENT_GUARD_CHECK cases=120000 original_and_candidate=equal "
            "guard_pages=passed callee_saved=passed side_effect_fallback=passed "
            "matched=4593 fallback=18433 null=4617 rejected=92357"})
    rows, gates = [], []
    for number, name in enumerate(gate.CASES):
        case = root / name
        old = gate.read(reference / name / "settings.json")
        settings = deepcopy(old)
        settings["observer"] = str(observer)
        settings["identities"][str(observer)] = gate.OBSERVER_SHA256
        save(case / "settings.json", settings)
        commands = len(gate.read(settings["replay"])["cmd"])
        lines = runner.optimization_installation_markers(gate.VARIANTS["candidate"])
        lines.append("HEADLESS_CREATE_RETURN_NULL=false")
        for batch in range(1, 7):
            lines.extend([f"HEADLESS_SERIAL_BATCH_BEGIN index={batch} batches=6", "HEADLESS_TASK_TID=123"])
            for match in range(1, 19):
                start = ((batch - 1) * 18 + match) * 2000
                phase = (match - 1) % 16
                count = (17 - phase + 15) // 16
                lines.extend([f"HEADLESS_BATTLE_METRICS completed=17 wall_ns=1000 step_total_ns=900 step_min_ns=1 step_max_ns=90 p50_ns=5 p95_ns=20 p99_ns=70 peak_rss_kb=2048 trace=0 start_ns={start} end_ns={start + 1000} sample_count={count} sample_every=16 sample_phase={phase} sample_phase_period=16 loaded_commands={commands} injected_commands=0",
                    "HEADLESS_BATTLE_MEMORY rss_kb=2048 pss_kb=1024",
                    "HEADLESS_GAME_STATE_STEP_RETURN tick_before=0 tick_after=17 completed=17 requested=10000 finalized=1",
                    f"HEADLESS_BATTLE_RESET iteration={match} repeats=18 digest=e16801510db89efd bytes=3 equal=1 validated=1"])
            lines.extend(["HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1",
                          f"HEADLESS_SERIAL_BATCH_END index={batch} batches=6 status=1"])
        lines.extend(["HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1", "HEADLESS_REJECTED_PM_CALLS=0",
                      "HEADLESS_BATTLE_READY=UNVERIFIED"])
        text = chr(10).join(lines)
        directory = case / "sustained-candidate"
        plan = runner.run(settings, directory, benchmark_repeats=18, serial_batches=6,
            timeout_seconds=1800, native_optimization=gate.VARIANTS["candidate"], compress_stdout=True)
        save(directory / "plan.json", plan)
        (directory / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
        (directory / "exit.txt").write_text("0")
        result = gate.result_expected("candidate", 17, variants=gate.VARIANTS)
        result.update(matches=108, serial_batches=6)
        save(directory / "result.json", result)
        save(case / "sustained-summary.json", gate.summarize(text, 6, 18, 17, "e16801510db89efd", 3, 2))
        save(case / "sustained-environment.json", env(number * 100 + 1))
        save(case / "sustained-end-environment.json", env(number * 100 + 99))
        rows.append(dict(case=name, result=result))
        for variant in gate.VARIANTS:
            source = reference / name / "full-original"
            directory = case / ("post-full-" + variant)
            plan = deepcopy(gate.read(source / "plan.json"))
            plan["command"] = [x.replace(plan["output"], str(directory)).replace(old["observer"], str(observer)) for x in plan["command"]]
            plan["environment"] = {k: v.replace(plan["output"], str(directory)) for k, v in plan["environment"].items()}
            if gate.VARIANTS[variant]:
                plan["environment"]["NASHURA_NATIVE_OPTIMIZATION"] = gate.VARIANTS[variant]
            plan.update(output=str(directory), identities=settings["identities"], native_optimization=gate.VARIANTS[variant])
            save(directory / "plan.json", plan)
            text = gzip.decompress((source / "stdout.log.gz").read_bytes()).decode()
            text += f"\nHEADLESS_BATTLE_METRICS loaded_commands={commands} injected_commands=0"
            if gate.VARIANTS[variant]:
                text = chr(10).join(runner.optimization_installation_markers(gate.VARIANTS[variant])) + chr(10) + text
            (directory / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
            shutil.copyfile(source / "snapshots.bin.gz", directory / "snapshots.bin.gz")
            (directory / "exit.txt").write_text("0")
            result = gate.read(source / "result.json")
            result["native_optimization"] = gate.VARIANTS[variant]
            save(directory / "result.json", result)
            gates.append(dict(case=name, variant=variant, result=result,
                snapshot_gzip_sha256=gate.sha256(directory / "snapshots.bin.gz"),
                stdout_gzip_sha256=gate.sha256(directory / "stdout.log.gz")))
    save(root / "sustained-results.json", rows)
    save(root / "correctness-results.json", gates)
    return root, reference


def test_complete_graph_and_tampered_evidence(inputs, monkeypatch):
    root, reference = synthetic(inputs, monkeypatch)
    result = gate.verify(root, reference)
    assert result["total_matches"] == 432 and len(result["post_full"]) == 8
    assert result["whole_goal_complete"] is False
    for name, mutate in [
        ("done.json", lambda x: x.update(successes=11)),
        ("stage-plan.json", lambda x: x.update(observer_sha256="0" * 64)),
        ("instruction-check.json", lambda x: x.update(harness_sha256="0" * 64)),
        ("post-full-environment.json", lambda x: x.update(epoch_ns=300)),
        (gate.CASES[0] + "/sustained-summary.json", lambda x: x["memory_trends"]["pss"].update(last_kib=0)),
        (gate.CASES[0] + "/sustained-candidate/plan.json", lambda x: x["environment"].update(NASHURA_PHASE_TIMING="1")),
        (gate.CASES[1] + "/sustained-environment.json", lambda x: x.update(epoch_ns=1)),
        (gate.CASES[0] + "/post-full-candidate/result.json", lambda x: x.update(archive_sha256="0" * 64)),
    ]:
        path = root / name
        original = path.read_text()
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            gate.verify(root, reference)
        path.write_text(original)
