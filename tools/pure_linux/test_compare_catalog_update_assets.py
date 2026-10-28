import json

import pytest

from compare_catalog_update_assets import audit, compare, parse_inventory, sha
from test_catalog_resource_audit import fixture as trace_fixture


def test_compares_all_and_opened_resources():
    values = compare({"a": "1", "b": "2"}, {"a": "1", "b": "3"}, ["a", "b"])
    assert values["sha_mismatches"] == values["opened_sha_mismatches"] == 1
    assert values["opened_calls"] == 2


@pytest.mark.parametrize("name", ["../bad", "a/../b", "a//b", "/absolute"])
def test_invalid_paths_are_rejected(tmp_path, name):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(["a" * 64 + "  ./" + name]))
    with pytest.raises(ValueError):
        parse_inventory(path)


def test_duplicate_and_unbound_are_rejected(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(["a" * 64 + "  ./x"] * 2))
    with pytest.raises(ValueError, match="duplicate"):
        parse_inventory(path)
    with pytest.raises(ValueError, match="not bound"):
        compare({"x": "a"}, {"x": "a"}, ["missing"])


def test_audit_binds_and_rejects_tampered_trace(tmp_path):
    result, trace, stderr = trace_fixture(tmp_path)
    record = json.loads(result.read_text())
    record["resources_before"]["update"]["a"] = "a" * 64
    record["resources_after"]["update"]["a"] = "a" * 64
    result.write_text(json.dumps(record))
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(["a" * 64 + "  ./a"]))
    after.write_bytes(before.read_bytes())
    paths = (before, after, result, trace, stderr)
    names = ("android_before", "android_after", "loader_result", "loader_trace", "loader_stderr")
    expected = dict(zip(names, map(sha, paths)))
    roots = {"assets": "/fixture/assets", "update": "/fixture/update"}
    assert audit(*paths, expected, roots, "/fixture")["status"] == "PASS_LIMITED"
    trace.write_text(trace.read_text() + "tampered\n")
    with pytest.raises(ValueError, match="identity"):
        audit(*paths, expected, roots, "/fixture")
