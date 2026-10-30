"""Independent checks for compressed raw evidence and paired batch accounting."""
import gzip
import json
from copy import deepcopy

import pytest

import run_headless_match as runner
import verify_combined_followup as followup
from test_run_headless_match import inputs, COMPLETE
from test_headless_fourway import synthetic_evidence


def batches():
    return [{"index": i, "variant": variant, "summary": {
        "median_match_ms": 100 if variant == "original" else 92,
        "measured_matches": 200, "tick": 3601,
        "timed_seconds": 20 if variant == "original" else 18.4,
        "step_percentiles_us": {"p50": 5, "p95": 10, "p99": 20}}}
        for i, variant in enumerate(followup.ORDER, 1)]


def test_paired_block_statistics_keep_batch_units():
    result = followup.aggregate(batches())
    assert result["duration_change_percent"] == pytest.approx(-8)
    assert result["throughput_change_percent"] == pytest.approx((20 / 18.4 - 1) * 100)
    assert result["block_duration_change_percent"] == pytest.approx([-8, -8])
    assert result["variants"]["combined"]["batches"] == 4


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reverse", "index"])
def test_missing_or_unbalanced_batches_rejected(mutation):
    rows = batches()
    if mutation == "missing": rows.pop()
    elif mutation == "duplicate": rows[-1] = deepcopy(rows[0])
    elif mutation == "reverse": rows.reverse()
    else: rows[-1]["index"] = 9
    with pytest.raises(ValueError, match="ABBA/BAAB"):
        followup.aggregate(rows)


@pytest.mark.parametrize("mutation", [None, "missing", "duplicate", "reverse", "diagnostic", "raw"])
def test_compressed_logs_preserve_all_marker_and_quiet_requirements(tmp_path, mutation):
    lines = runner.optimization_installation_markers("hot-distance-v1")
    if mutation == "missing": lines.pop()
    elif mutation == "duplicate": lines.append(lines[-1])
    elif mutation == "reverse": lines.reverse()
    elif mutation == "diagnostic": lines.append("HEADLESS_BATTLE_OBJECT id=1")
    text = chr(10).join(lines)
    (tmp_path / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
    if mutation == "raw": (tmp_path / "stdout.log").write_text(text)
    if mutation:
        with pytest.raises(ValueError): followup.stdout(tmp_path, "combined", timing=True)
    else:
        assert followup.stdout(tmp_path, "combined", timing=True) == text


def test_zero_controller_exit_does_not_replace_correctness_gate(tmp_path):
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata/nashura-combined-followup-control-20261031-v1.exit").write_text("0")
    root = tmp_path / "experiment"; root.mkdir()
    (root / "correctness-done.json").write_text(json.dumps({"runs": 8, "successes": 7}))
    with pytest.raises(ValueError, match="complete correctness"):
        followup.verify(root, tmp_path, tmp_path)


@pytest.mark.parametrize("counts", [(51,0),(52,1),(0,0),(-1,0)])
def test_exact_fixed_action_counts_required_even_when_nonempty(counts):
    replay = {"cmd":[{}] * 52,"inject":[]}
    text = f"HEADLESS_BATTLE_METRICS loaded_commands={counts[0]} injected_commands={counts[1]}"
    with pytest.raises(ValueError,match="action sequence"):
        followup.validate_actions(text,replay)


def combined_evidence(inputs, monkeypatch):
    previous, reference = synthetic_evidence(inputs, monkeypatch)
    root = previous.parent / "followup"
    root.mkdir()
    (root.parent / "metadata/nashura-combined-followup-control-20261031-v1.exit").write_text("0")
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    def result(variant, *, repeats=1, batches=1, archive=None):
        value = {"status": "NATIVE_FINAL_OBSERVED_LIMITED", "final_tick": 17,
            "snapshot_trace": archive is not None, "matches": repeats * batches,
            "record_hand_actions": False, "native_optimization": followup.VARIANTS[variant],
            "native_phases": False, "thread_resources": False, "serial_batches": batches,
            "whole_goal_complete": False}
        if archive:
            value.update(records=18, archive_bytes=len(archive),
                         archive_sha256=followup.hashlib.sha256(archive).hexdigest())
        return value
    complete = COMPLETE.replace("3601", "17")
    def log(variant, repeats, commands, batches=1):
        lines = runner.optimization_installation_markers(followup.VARIANTS[variant]) if variant != "original" else []
        if batches > 1:
            lines.append("HEADLESS_CREATE_RETURN_NULL=false")
        for batch in range(1, batches + 1):
            if batches > 1:
                lines.extend([f"HEADLESS_SERIAL_BATCH_BEGIN index={batch} batches={batches}", "HEADLESS_TASK_TID=123"])
            for iteration in range(1, repeats + 1):
                phase = (iteration - 1) % 16
                samples = (17 - phase + 15) // 16
                start = ((batch - 1) * repeats + iteration) * 2000
                lines.extend([f"HEADLESS_BATTLE_METRICS completed=17 wall_ns=1000 step_total_ns=900 step_min_ns=1 step_max_ns=90 p50_ns=5 p95_ns=20 p99_ns=70 peak_rss_kb=2048 trace=0 start_ns={start} end_ns={start + 1000} sample_count={samples} sample_every=16 sample_phase={phase} sample_phase_period=16 loaded_commands={commands} injected_commands=0",
                    "HEADLESS_BATTLE_MEMORY rss_kb=2048 pss_kb=1024", complete.splitlines()[0],
                    f"HEADLESS_BATTLE_RESET iteration={iteration} repeats={repeats} digest=e16801510db89efd bytes=3 equal=1 validated=1"])
            if batches > 1:
                lines.extend(["HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1",
                    f"HEADLESS_SERIAL_BATCH_END index={batch} batches={batches} status=1"])
        lines.extend(complete.splitlines()[2:] if batches > 1 else complete.splitlines()[1:])
        lines.append("HEADLESS_BATTLE_READY=UNVERIFIED")
        return chr(10).join(lines)
    gates, timings, long = [], [], []
    for name in followup.CASES:
        settings = followup.read(previous / name / "settings.json")
        commands = len(followup.read(settings["replay"])["cmd"])
        save(root / name / "settings.json", settings)
        for variant, old in (("original", "a"), ("combined", "d")):
            source, target = previous / name / old, root / name / ("full-" + variant)
            plan = followup.read(source / "plan.json")
            prior = plan["output"]
            plan["command"] = [x.replace(prior, str(target)) for x in plan["command"]]
            plan["environment"] = {k:v.replace(prior, str(target)) for k,v in plan["environment"].items()}
            plan.update(output=str(target), stdout_encoding="gzip-after-process-exit")
            save(target / "plan.json", plan)
            text = (source / "stdout.log").read_text()
            (target / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
            archive = (source / "snapshots.bin").read_bytes()
            (target / "snapshots.bin.gz").write_bytes(gzip.compress(archive))
            (target / "exit.txt").write_text("0")
            value = result(variant, archive=archive)
            save(target / "result.json", value)
            gates.append({"case":name, "variant":variant, "result":value, "frames":18,
                "observed_lines":1, "full_frames_equal":True, "observed_equal":True,
                "snapshot_gzip_sha256":followup.sha256(target / "snapshots.bin.gz"),
                "stdout_gzip_sha256":followup.sha256(target / "stdout.log.gz")})
        for index, variant in enumerate(followup.ORDER, 1):
            label = str(index).zfill(2) + "-" + variant
            target = root / name / "timing" / label
            plan = runner.run(settings, target, benchmark_repeats=216, timeout_seconds=120,
                              native_optimization=followup.VARIANTS[variant], compress_stdout=True)
            save(target / "plan.json", plan)
            text = log(variant, 216, commands)
            (target / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
            (target / "exit.txt").write_text("0")
            summary = followup.compact(followup.benchmark(text,216,expected_tick=17,
                expected_digest="e16801510db89efd",expected_bytes=3,warmup_matches=16))
            value = result(variant, repeats=216)
            save(target / "result.json", value)
            save(target.parent / (label + "-summary.json"), summary)
            save(target.parent / (label + "-environment.json"), {"cpu_count":2,"affinity":[0,1],"epoch_ns":index})
            timings.append({"case":name,"index":index,"variant":variant,"result":value,"summary":summary})
        target = root / name / "long-combined"
        plan = runner.run(settings,target,benchmark_repeats=516,serial_batches=24,timeout_seconds=600,
                          native_optimization="hot-distance-v1",compress_stdout=True)
        save(target / "plan.json",plan)
        text = log("combined",516,commands,24)
        (target / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
        (target / "exit.txt").write_text("0")
        value = result("combined",repeats=516,batches=24)
        save(target / "result.json",value)
        summary = followup.stability(text,24,516,17,"e16801510db89efd",3)
        save(target.parent / "long-summary.json",summary)
        save(target.parent / "long-environment.json",{"cpu_count":2,"affinity":[0,1],"epoch_ns":9})
        long.append({"case":name,"result":value,"matches":12384,"rss_kib":summary["rss_kib"],
            "observed_seconds":summary["first_to_last_loop_seconds"]})
    for filename, value in (("correctness-results.json",gates),("timing-results.json",timings),
        ("long-results.json",long),("correctness-done.json",{"runs":8,"successes":8}),
        ("timing-done.json",{"runs":32,"successes":32}),("done.json",{"runs":4,"successes":4})):
        save(root / filename,value)
    return root,reference,previous


def test_complete_evidence_and_tampered_result_or_actions_rejected(inputs,monkeypatch):
    root,reference,previous = combined_evidence(inputs,monkeypatch)
    value = followup.verify(root,reference,previous)
    assert len(value["correctness"]) == 8 and value["serial_matches"] == 49536
    assert value["lifetimes"][0]["mainloop_tid"] == 123
    assert value["whole_goal_complete"] is False
    target = root / followup.CASES[0] / "timing/01-original/result.json"
    saved = target.read_text()
    changed = json.loads(saved);changed["matches"] = 215
    target.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match="saved evidence"):
        followup.verify(root,reference,previous)
    target.write_text(saved)
    target = target.with_name("stdout.log.gz")
    raw = gzip.decompress(target.read_bytes())
    target.write_bytes(gzip.compress(raw.replace(b"loaded_commands=52",b"loaded_commands=0",1)))
    with pytest.raises(ValueError,match="action sequence"):
        followup.verify(root,reference,previous)
    target.write_bytes(gzip.compress(raw))
    target = root / followup.CASES[0] / "long-combined/stdout.log.gz"
    raw = gzip.decompress(target.read_bytes())
    target.write_bytes(gzip.compress(raw.replace(b"HEADLESS_TASK_TID=123",b"HEADLESS_TASK_TID=124",1)))
    with pytest.raises(ValueError,match="Mainloop thread"):
        followup.verify(root,reference,previous)
    target.write_bytes(gzip.compress(raw))
    target.with_name("exit.txt").write_text("70")
    with pytest.raises(ValueError,match="runtime exit code"):
        followup.verify(root,reference,previous)
