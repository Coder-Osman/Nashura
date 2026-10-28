import copy

import pytest

from audit_lookup_inner import compare_inner
from audit_finish_lookup_key import compare_key
from test_audit_finish_lookup import fixture as lookup_fixture


def fixture():
    android, linux = lookup_fixture()
    outer = next(row for row in android["trace"]["events"] if row["kind"] == "finish_lookup")
    outer.update(sequence=103, lookupInput={"first_nonnull": True, "nested_nonnull": True}, key="fixture")
    after = next(row for row in android["trace"]["events"] if row.get("phase") == "finish_a" and row.get("edge") == "after")
    after["sequence"] = 104
    android["trace"]["events"].append({"kind": "lookup_inner", "batch": outer["batch"],
                                       "thread": outer["thread"], "sequence": 102, "nonnull": True})
    linux["events"][-1].update(lookup_input={"first_nonnull": True, "nested_nonnull": True},
                               strings={"x1": {"encoding": "engine-string16", "terminated": True,
                                               "length": 7, "bytes_hex": b"fixture".hex()}})
    linux["events"].append({"name": "lookup_inner", "thread": linux["events"][-1]["thread"], "return_x0": 0})
    return android, linux


def test_comparison_records_observation_without_pointer_contents():
    android, linux = fixture()
    result = compare_inner(android, linux)
    assert result["android_inner_nonnull"] is True
    assert result["linux_inner_nonnull"] is False
    assert compare_key(android, linux)["same_bytes"] is True


@pytest.mark.parametrize("damage", ["thread", "sequence", "return", "unknown"])
def test_incomplete_observation_is_rejected(damage):
    android, linux = fixture()
    if damage == "thread":
        linux["events"][-1]["thread"] = [9]
    elif damage == "sequence":
        android["trace"]["events"][-1]["sequence"] = 100
    elif damage == "return":
        del linux["events"][-1]["return_x0"]
    else:
        linux["events"][-2]["lookup_input"]["nested_nonnull"] = None
    with pytest.raises(ValueError):
        compare_inner(android, linux)


def test_different_keys_are_reported():
    android, linux = fixture()
    changed = copy.deepcopy(linux)
    changed["events"][-2]["strings"]["x1"]["bytes_hex"] = b"changed".hex()
    assert compare_key(android, changed)["same_bytes"] is False
