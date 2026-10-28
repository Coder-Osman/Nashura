import json

import pytest

from audit_catalog_update_drift import audit, sha
from test_catalog_resource_audit import fixture


def test_repeated_drift_and_input_tampering(tmp_path):
    _, trace, _ = fixture(tmp_path)
    reference = tmp_path / "reference.json"
    layout = {"tables": [[1, 2, 2, 2, -1, -1]], "totalRows": 2}
    reference.write_text(json.dumps({"status": "RUNTIME_OBSERVED_LIMITED", "layout": layout}))
    roots = {"assets": "/fixture/assets", "update": "/fixture/update"}
    directories = [tmp_path / "v4", tmp_path / "v5"]
    before = json.dumps(["a" * 64 + "  ./a", "b" * 64 + "  ./missing.sc"])
    after = json.dumps(["a" * 64 + "  ./a"])
    for path in directories:
        path.mkdir()
        (path / "update-before.json").write_text(before)
        (path / "update-after.json").write_text(after)
    expected_before = sha(directories[0] / "update-before.json")
    for path in directories:
        (path / "result.json").write_text(json.dumps({"status": "FAILED", "catalog_ready_pass": False,
            "update_reference_sha256": expected_before, "layout": layout,
            "error": "ValueError: update inventory after capture differs"}))
    inputs = {"reference_layout": reference, "v4_result": directories[0] / "result.json",
              "v4_before": directories[0] / "update-before.json", "v4_after": directories[0] / "update-after.json",
              "v5_result": directories[1] / "result.json", "v5_before": directories[1] / "update-before.json",
              "v5_after": directories[1] / "update-after.json", "linux_trace": trace}
    expected = {name: sha(path) for name, path in inputs.items()}
    report = audit(reference, *directories, trace, expected, roots, "/fixture")
    assert report["removed_count"] == 1 and report["removed_also_opened_by_linux"] == 0
    trace.write_text(trace.read_text() + "tampered")
    with pytest.raises(ValueError, match="identity"):
        audit(reference, *directories, trace, expected, roots, "/fixture")
