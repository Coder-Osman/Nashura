"""Exercise the complete synthetic graph plus candidate-specific rejection."""
import json

import pytest

import verify_query_signed_height as gate
import verify_grid_clear_unroll as serial
from test_run_headless_match import inputs
from test_verify_grid_clear_unroll import synthetic


@pytest.mark.parametrize("confirmation", [False, True])
def test_complete_graph_and_tampering(inputs, monkeypatch, confirmation):
    monkeypatch.setattr(serial, "VARIANTS", gate.VARIANTS)
    root, reference = synthetic(inputs, monkeypatch)
    monkeypatch.setattr(gate, "GUEST_ROOT", serial.GUEST_ROOT)
    monkeypatch.setattr(gate, "CONFIRM_GUEST_ROOT", serial.GUEST_ROOT)
    monkeypatch.setattr(gate, "OBSERVER", serial.OBSERVER)
    monkeypatch.setattr(gate, "OBSERVER_SHA256", serial.sha256(serial.OBSERVER))
    monkeypatch.setattr(gate, "HARNESS_SHA256", "a" * 64)
    if confirmation:
        path = root / "stage-plan.json"
        value = json.loads(path.read_text())
        value["timing_case_order"] = list(reversed(serial.CASES))
        path.write_text(json.dumps(value))
        path = root / "timing-results.json"
        rows = json.loads(path.read_text())
        rows = [row for name in reversed(serial.CASES) for row in rows if row["case"] == name]
        path.write_text(json.dumps(rows))
        for number, row in enumerate(rows, 1):
            path = root / row["case"] / "timing" / (str(row["index"]).zfill(2) + "-" + row["variant"] + "-environment.json")
            value = json.loads(path.read_text())
            value["epoch_ns"] = number
            path.write_text(json.dumps(value))
    def verify():
        return gate.verify(root, reference, confirmation=confirmation)
    marker = "QUERY_SIGNED_HEIGHT_CHECK cases=192424 original_and_candidate=equal " \
        "guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3"
    for filename, when in (("instruction-precheck.json", "before_formal_timing"),
                           ("instruction-check.json", "after_formal_timing")):
        (root / filename).write_text(json.dumps(dict(exit_code=0, stderr="", stdout=marker,
            harness_sha256="a" * 64, **{when: True})))
    result = verify()
    assert len(result["correctness"]) == 12 and result["timing_batches"] == 32
    assert result["instruction_checks_before_and_after"] is True
    assert result["schema"] == ("nashura.query-signed-height-confirmation.v1" if confirmation
                                else "nashura.query-signed-height.v1")
    for filename, mutate in [
        ("instruction-precheck.json", lambda x: x.update(exit_code=1)),
        ("instruction-check.json", lambda x: x.update(harness_sha256="b" * 64)),
        ("instruction-check.json", lambda x: x.update(stdout=x["stdout"] + chr(10) + marker)),
        ("stage-plan.json", lambda x: x.update(profiling=True)),
        ("stage-plan.json", lambda x: x.update(observer_sha256="0" * 64)),
        ("stage-plan.json", lambda x: x.update(timing_case_order=[serial.CASES[0]] * 4)),
        ("correctness-done.json", lambda x: x.update(successes=11)),
        (serial.CASES[0] + "/full-candidate/result.json", lambda x: x.update(archive_sha256="0" * 64)),
        (serial.CASES[0] + "/timing/02-candidate/plan.json",
         lambda x: x["environment"].update(NASHURA_MEMORY_LIFECYCLE_EVERY="1")),
    ]:
        path = root / filename
        original = path.read_text()
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            verify()
        path.write_text(original)
