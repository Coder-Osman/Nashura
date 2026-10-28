from collections import Counter
import hashlib
import json
import zipfile

import pytest

from compare_catalog_apk_assets import audit, compare, sha


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            archive.writestr("assets/" + name, data)


def test_compares_content_and_staged_identity(tmp_path):
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(first, [("a", b"same"), ("b", b"old")])
    make_zip(second, [("a", b"same"), ("b", b"new")])
    values = {"a": hashlib.sha256(b"same").hexdigest(), "b": "wrong"}
    result = compare(first, second, values, Counter({"a": 2}))
    assert result["different_asset_bytes"] == 1 and result["staged_manifest_mismatches"] == 1
    assert result["loader_asset_open_calls"] == 2 and result["loader_unique_asset_paths"] == 1


def test_rejects_duplicate_paths(tmp_path):
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    with pytest.warns(UserWarning):
        make_zip(first, [("a", b"x"), ("a", b"y")])
    make_zip(second, [("a", b"x")])
    with pytest.raises(ValueError, match="duplicate"):
        compare(first, second, {}, Counter())


def test_rejects_unbound_open(tmp_path):
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(first, [("a", b"x")])
    make_zip(second, [("a", b"x")])
    with pytest.raises(ValueError, match="missing"):
        compare(first, second, {}, Counter({"a": 1}))


def test_audit_binds_inputs_and_loader_identity(tmp_path):
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    make_zip(first, [("a", b"same")])
    make_zip(second, [("a", b"same")])
    stderr = tmp_path / "stderr.log"
    stderr.write_text("NATIVE_ASSET_OPEN\ta\t4\n")
    reference = tmp_path / "reference.tsv"
    reference.write_text("summary\tfiles=9829\tbytes=849482445\tfailures=0\tdescriptors=9829\tmissing=2\n")
    resource = {"assets": {"a": hashlib.sha256(b"same").hexdigest()}}
    loader = tmp_path / "result.json"
    loader.write_text(json.dumps({"exit_code": 0, "inputs_unchanged": True, "resources_unchanged": True,
                                  "postcheck_errors": [], "resources_before": resource,
                                  "resources_after": resource, "outputs": {"stderr.log": sha(stderr)}}))
    names = ("stock_apk", "current_apk", "loader_result", "loader_stderr", "e1_comparison")
    paths = (first, second, loader, stderr, reference)
    expected = dict(zip(names, map(sha, paths)))
    assert audit(*paths, expected)["status"] == "PASS_LIMITED"
    stderr.write_text("NATIVE_ASSET_OPEN\ta\t5\n")
    with pytest.raises(ValueError, match="identity"):
        audit(*paths, expected)
    expected["loader_stderr"] = sha(stderr)
    with pytest.raises(ValueError, match="loader evidence"):
        audit(*paths, expected)
