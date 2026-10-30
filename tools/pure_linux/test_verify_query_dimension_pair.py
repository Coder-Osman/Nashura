"""Exercise query-specific identities, markers and the shared complete graph."""
import gzip
import json
import pytest

import verify_query_dimension_pair as query
import verify_grid_clear_unroll as serial
from test_run_headless_match import inputs
from test_verify_grid_clear_unroll import synthetic


@pytest.mark.parametrize("confirmation", [False, True])
def test_query_graph_and_tampering(inputs, monkeypatch, confirmation):
    monkeypatch.setattr(serial, "VARIANTS", query.VARIANTS)
    root, reference = synthetic(inputs, monkeypatch)
    monkeypatch.setattr(query, "GUEST_ROOT", serial.GUEST_ROOT)
    monkeypatch.setattr(query, "CONFIRM_GUEST_ROOT", serial.GUEST_ROOT)
    monkeypatch.setattr(query, "OBSERVER", serial.OBSERVER)
    monkeypatch.setattr(query, "OBSERVER_SHA256", serial.sha256(serial.OBSERVER))
    (root / "instruction-check.json").write_text(json.dumps({
        "exit_code": 0, "stderr": "", "after_formal_timing": True,
        "stdout": "QUERY_DIMENSION_PAIR_CHECK cases=192424 original_and_candidate=equal "
        "guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3"}))
    if confirmation:
        path = root / "stage-plan.json"
        plan = json.loads(path.read_text())
        plan["timing_case_order"] = list(reversed(serial.CASES))
        path.write_text(json.dumps(plan))
        path = root / "timing-results.json"
        rows = json.loads(path.read_text())
        rows = [row for name in reversed(serial.CASES) for row in rows if row["case"] == name]
        path.write_text(json.dumps(rows))
        for number, row in enumerate(rows, 1):
            path = root / row["case"] / "timing" / (str(row["index"]).zfill(2) + "-" + row["variant"] + "-environment.json")
            env = json.loads(path.read_text())
            env["epoch_ns"] = number
            path.write_text(json.dumps(env))
    def verify():
        return query.verify(root, reference, confirmation=confirmation)
    result = verify()
    assert result["schema"] == ("nashura.query-dimension-pair-confirmation.v1"
        if confirmation else "nashura.query-dimension-pair.v1")
    assert len(result["correctness"]) == 12
    assert result["timing_batches"] == 32
    assert result["instruction_cases"] == 192424
    assert result["whole_goal_complete"] is False
    targets = [
        ("stage-plan.json", lambda v: v.update(timing_case_order=[serial.CASES[0]] * 4)),
        (serial.CASES[0] + "/timing/01-combined-environment.json", lambda v: v.update(epoch_ns=999999)),
        ("instruction-check.json", lambda v: v.update(exit_code=1)),
        ("instruction-check.json", lambda v: v.update(after_formal_timing=False)),
        ("stage-plan.json", lambda v: v["variants"].update(candidate="hot-distance-grid-v1")),
        ("stage-plan.json", lambda v: v.update(concurrency=True)),
        ("correctness-done.json", lambda v: v.update(successes=11)),
        (serial.CASES[0] + "/full-candidate/result.json", lambda v: v.update(archive_sha256="0" * 64)),
        (serial.CASES[0] + "/timing/02-candidate/plan.json",
         lambda v: v["environment"].update(NASHURA_NATIVE_PHASES="1")),
    ]
    if confirmation:
        targets.append(("stage-plan.json", lambda v: v.update(observer_sha256="0" * 64)))
    for rel, mutate in targets:
        path = root / rel
        original = path.read_text()
        value = json.loads(original)
        mutate(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            verify()
        path.write_text(original)
    path = root / serial.CASES[0] / "timing/02-candidate/stdout.log.gz"
    original = path.read_bytes()
    text = gzip.decompress(original).decode()
    text = chr(10).join(x for x in text.splitlines()
        if not x.startswith("HEADLESS_NATIVE_OPTIMIZATION name=query-dimension-pair-v1 "))
    path.write_bytes(gzip.compress(text.encode()))
    with pytest.raises(ValueError, match="installation markers"):
        verify()
