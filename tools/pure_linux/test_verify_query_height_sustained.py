"""Reuse the complete graph and reject child/plan/clock/state corruption."""
import json

import pytest

import verify_component_sustained as serial
import verify_query_height_sustained as gate
from test_run_headless_match import inputs
from test_verify_component_sustained import synthetic


def test_complete_graph_and_tampering(inputs, monkeypatch):
    monkeypatch.setattr(serial, "VARIANTS", gate.VARIANTS)
    root, reference = synthetic(inputs, monkeypatch)
    monkeypatch.setattr(gate, "GUEST_ROOT", serial.GUEST_ROOT)
    monkeypatch.setattr(gate, "OBSERVER", serial.OBSERVER)
    monkeypatch.setattr(gate, "OBSERVER_SHA256", serial.OBSERVER_SHA256)
    marker = "QUERY_SIGNED_HEIGHT_CHECK cases=192424 original_and_candidate=equal " \
        "guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3"
    for name, when in (("instruction-precheck.json", "before_formal_timing"),
                       ("instruction-check.json", "after_formal_timing")):
        p = root / name
        component = json.loads(p.read_text())
        height = dict(exit_code=0, stderr="", stdout=marker,
            harness_sha256=gate.HARNESS_SHA256, **{when: True})
        p.write_text(json.dumps(dict(exit_code=0, component=component, signed_height=height,
                                    **{when: True})))
    result = gate.verify(root, reference)
    assert result["schema"] == "nashura.query-height-sustained.v1"
    assert result["total_matches"] == 432 and len(result["post_full"]) == 8
    assert result["whole_goal_complete"] is False
    for name, mutate in [
        ("instruction-precheck.json", lambda x: x["component"].update(exit_code=1)),
        ("instruction-check.json", lambda x: x["signed_height"].update(exit_code=1)),
        ("instruction-check.json", lambda x: x["signed_height"].update(harness_sha256="0" * 64)),
        ("instruction-check.json", lambda x: x["signed_height"].update(stdout=marker + chr(10) + marker)),
        ("stage-plan.json", lambda x: x.update(optimization="hot-distance-component-v1")),
        ("stage-plan.json", lambda x: x.update(concurrency=True)),
        (serial.CASES[0] + "/sustained-candidate/plan.json",
         lambda x: x["environment"].update(NASHURA_MEMORY_LIFECYCLE_EVERY="1")),
        (serial.CASES[1] + "/sustained-environment.json", lambda x: x.update(epoch_ns=1)),
        (serial.CASES[0] + "/post-full-candidate/result.json",
         lambda x: x.update(archive_sha256="0" * 64)),
    ]:
        p = root / name
        original = p.read_text()
        value = json.loads(original)
        mutate(value)
        p.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            gate.verify(root, reference)
        p.write_text(original)
