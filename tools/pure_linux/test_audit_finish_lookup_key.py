import copy

import pytest

from audit_finish_lookup_key import compare_key


def fixture():
    android = {"trace": {"events": [{"kind": "finish_lookup", "key": "fixture"}]}}
    linux = {"events": [{"name": "finish_lookup", "strings": {"x1": {"encoding": "engine-string16",
                        "terminated": True, "length": 7, "bytes_hex": b"fixture".hex()}}}]}
    return android, linux


def test_reports_equality_without_exposing_key():
    android, linux = fixture()
    result = compare_key(android, linux)
    assert result["same_bytes"] and result["android_length"] == 7
    assert "fixture" not in str(result)
    changed = copy.deepcopy(linux)
    changed["events"][0]["strings"]["x1"]["bytes_hex"] = b"changed".hex()
    assert not compare_key(android, changed)["same_bytes"]


def test_rejects_missing_and_bad_length():
    android, linux = fixture()
    linux["events"][0]["strings"]["x1"]["length"] = 8
    with pytest.raises(ValueError, match="bounds"):
        compare_key(android, linux)
    android["trace"]["events"][0]["key"] = None
    with pytest.raises(ValueError, match="missing"):
        compare_key(android, linux)
