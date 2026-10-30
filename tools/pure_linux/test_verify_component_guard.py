"""Reject missing parity, mixed harnesses and incomplete experiment graphs."""
import gzip
import json
import pytest
import verify_component_guard as guard
import verify_grid_clear_unroll as serial
from test_run_headless_match import inputs
from test_verify_grid_clear_unroll import synthetic


def test_complete_graph_and_tampered_guard_evidence(inputs, monkeypatch):
    monkeypatch.setattr(serial, "VARIANTS", guard.VARIANTS)
    root, reference = synthetic(inputs, monkeypatch)
    monkeypatch.setattr(guard, "GUEST_ROOT", serial.GUEST_ROOT)
    monkeypatch.setattr(guard, "OBSERVER", serial.OBSERVER)
    for filename, timing in (("instruction-precheck.json", "before_formal_timing"),
                             ("instruction-check.json", "after_formal_timing")):
        (root / filename).write_text(json.dumps({
            "exit_code": 0, "stderr": "", timing: True, "harness_sha256": "a" * 64,
            "stdout": "COMPONENT_GUARD_CHECK cases=120000 original_and_candidate=equal "
                "guard_pages=passed callee_saved=passed side_effect_fallback=passed "
                "matched=4593 fallback=18433 null=4617 rejected=92357"}))
    result = guard.verify(root, reference)
    assert len(result["correctness"]) == 12 and result["timing_batches"] == 32
    assert result["instruction_cases"] == 120000
    assert result["instruction_checks_before_and_after"] is True
    assert result["whole_goal_complete"] is False
    targets = [
        ("instruction-precheck.json", lambda v: v.update(exit_code=1)),
        ("instruction-check.json", lambda v: v.update(after_formal_timing=False)),
        ("instruction-check.json", lambda v: v.update(harness_sha256="b" * 64)),
        ("instruction-check.json", lambda v: v.update(stdout=v["stdout"].replace("4593", "4592"))),
        ("stage-plan.json", lambda v: v.update(concurrency=True)),
        ("correctness-done.json", lambda v: v.update(successes=11)),
        (serial.CASES[0] + "/full-candidate/result.json", lambda v: v.update(archive_sha256="0" * 64)),
        (serial.CASES[0] + "/timing/02-candidate/plan.json",
         lambda v: v["environment"].update(NASHURA_NATIVE_PHASES="1")),
    ]
    for relative, mutate in targets:
        path = root / relative
        original = path.read_text()
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            guard.verify(root, reference)
        path.write_text(original)
    path = root / serial.CASES[0] / "timing/02-candidate/stdout.log.gz"
    original = gzip.decompress(path.read_bytes()).decode()
    text = chr(10).join(x for x in original.splitlines()
        if not x.startswith("HEADLESS_NATIVE_OPTIMIZATION name=component-guard-v1 "))
    path.write_bytes(gzip.compress(text.encode()))
    with pytest.raises(ValueError, match="installation markers"):
        guard.verify(root, reference)
