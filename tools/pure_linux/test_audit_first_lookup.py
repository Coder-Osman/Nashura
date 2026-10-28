import pytest

from audit_first_lookup import compare_first


def fixture():
    android = {"trace": {"events": [
        {"kind": "first_lookup_result", "batch": 1, "thread": 2, "sequence": 3, "found": True},
        {"kind": "first_lookup_result", "batch": 1, "thread": 2, "sequence": 4, "found": False},
        {"kind": "lookup_inner", "batch": 1, "thread": 2, "sequence": 5,
         "tree": {"root_nonnull": True, "size_word": "8"}}]}}
    linux = {"events": [
        {"name": "lookup_inner", "thread": [1, 2, 3], "first_lookup_tree": {"root_nonnull": False, "size_word": 0}},
        {"name": "first_lookup_result", "thread": [1, 2, 3], "first_lookup_found": False}]}
    return android, linux


def test_first_result_distinct_from_later_lookups():
    android, linux = fixture()
    result = compare_first(android, linux)
    assert result["android_first_found"] is True
    assert result["android_lookup_count"] == 2
    assert result["linux_root_nonnull"] is False


@pytest.mark.parametrize("damage", ["thread", "sequence", "unknown", "missing"])
def test_rejects_incomplete_tree_evidence(damage):
    android, linux = fixture()
    if damage == "thread":
        android["trace"]["events"][0]["thread"] = 5
    elif damage == "sequence":
        android["trace"]["events"][0]["sequence"] = 9
    elif damage == "unknown":
        linux["events"][0]["first_lookup_tree"]["root_nonnull"] = None
    else:
        linux["events"].pop()
    with pytest.raises(ValueError):
        compare_first(android, linux)
