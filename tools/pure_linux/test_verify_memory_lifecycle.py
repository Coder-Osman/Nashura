"""Synthetic rejection checks; real ARM64 evidence is a separate gate."""
import gzip
import json

import pytest

import verify_memory_lifecycle as gate
from test_headless_memory_lifecycle import fixture
from test_run_headless_match import inputs
from test_verify_component_sustained import synthetic as sustained


def synthetic(inputs, monkeypatch):
    root, reference = sustained(inputs, monkeypatch)
    observer = str(root.parent / "sustained-observer")
    monkeypatch.setattr(gate, "GUEST_ROOT", root)
    monkeypatch.setattr(gate, "OBSERVER", observer)
    monkeypatch.setattr(gate, "OBSERVER_SHA256", gate.sha256(observer))
    monkeypatch.setattr(gate, "BATCHES", 6)
    monkeypatch.setattr(gate, "REPEATS", 18)
    monkeypatch.setattr(gate, "INTERVAL", 17)
    def save(p, value):
        p.write_text(json.dumps(value))
    save(root / "stage-plan.json", dict(cases=list(gate.CASES), serial_batches=6,
        repeats=18, interval=17, observer=observer, observer_sha256=gate.OBSERVER_SHA256,
        scope="diagnostic-only", formal_timing=False, concurrency=False,
        engineering_checks_finished_before_launch=True))
    save(root / "done.json", dict(diagnostic_runs=4, full_runs=8, successes=12))
    save(root / "initial-environment.json", dict(cpu_count=2, affinity=[0, 1],
        memory_mib_expected=6144, epoch_ns=1))
    rows, gates = gate.read(root / "sustained-results.json"), gate.read(root / "correctness-results.json")
    for number, name in enumerate(gate.CASES):
        case = root / name
        old = case / "sustained-candidate"
        out = case / "diagnostic-candidate"
        old.rename(out)
        plan = gate.read(out / "plan.json")
        plan["command"] = [value.replace(str(old), str(out)).replace("1800s", "900s") for value in plan["command"]]
        plan.update(output=str(out), measurement_scope="memory-lifecycle-diagnostic-only")
        plan["environment"]["NASHURA_MEMORY_LIFECYCLE_EVERY"] = "17"
        save(out / "plan.json", plan)
        text = fixture(17, gzip.decompress((out / "stdout.log.gz").read_bytes()).decode())
        (out / "stdout.log.gz").write_bytes(gzip.compress(text.encode()))
        result = gate.read(out / "result.json")
        result["measurement_scope"] = "memory-lifecycle-diagnostic-only"
        save(out / "result.json", result)
        rows[number]["result"] = result
        save(case / "memory-summary.json", gate.summarize(text, 6, 18, 17, "e16801510db89efd", 3, 17))
        for source, target in (("sustained-environment.json", "diagnostic-environment.json"),
                               ("sustained-end-environment.json", "diagnostic-end-environment.json")):
            value = gate.read(case / source)
            value["epoch_ns"] += 1
            save(case / target, value)
        for variant in gate.VARIANTS:
            old, out = case / ("post-full-" + variant), case / ("full-" + variant)
            old.rename(out)
            plan = gate.read(out / "plan.json")
            plan["command"] = [value.replace(str(old), str(out)) for value in plan["command"]]
            plan["environment"] = {key: value.replace(str(old), str(out)) for key, value in plan["environment"].items()}
            plan["output"] = str(out)
            save(out / "plan.json", plan)
    save(root / "diagnostic-results.json", rows)
    return root, reference


def test_complete_graph_and_tampering(inputs, monkeypatch):
    root, reference = synthetic(inputs, monkeypatch)
    result = gate.verify(root, reference)
    assert result["total_matches"] == 432 and len(result["full"]) == 8
    assert result["benchmark"] is result["whole_goal_complete"] is False
    for name, mutate in [
        ("done.json", lambda x: x.update(successes=11)),
        ("stage-plan.json", lambda x: x.update(formal_timing=True)),
        ("stage-plan.json", lambda x: x.update(observer_sha256="0" * 64)),
        ("initial-environment.json", lambda x: x.update(cpu_count=4)),
        (gate.CASES[0] + "/diagnostic-candidate/plan.json", lambda x: x.pop("measurement_scope")),
        (gate.CASES[0] + "/diagnostic-candidate/plan.json", lambda x: x["environment"].update(NASHURA_PHASE_TIMING="1")),
        (gate.CASES[0] + "/memory-summary.json", lambda x: x["trends"]["post_destroy"]["heap_bytes"]["uordblks_bytes"].update(last=0)),
        (gate.CASES[1] + "/diagnostic-environment.json", lambda x: x.update(epoch_ns=1)),
        (gate.CASES[0] + "/full-candidate/result.json", lambda x: x.update(archive_sha256="0" * 64)),
    ]:
        path = root / name
        original = path.read_text()
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            gate.verify(root, reference)
        path.write_text(original)
