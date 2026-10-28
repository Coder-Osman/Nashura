import copy

import pytest

from audit_finish_lookup import compare
from test_audit_catalog_finish_phases import fixture as phase_fixture


def fixture():
    android, linux = phase_fixture()
    starts = [row for row in android["trace"]["events"] if row["kind"] == "table_batch"]
    before = next(row for row in android["trace"]["events"] if row.get("phase") == "finish_a" and row.get("edge") == "before")
    after = next(row for row in android["trace"]["events"] if row.get("phase") == "finish_a" and row.get("edge") == "after")
    before["contextSlotB60Nonnull"] = True
    before["sequence"], after["sequence"] = 101, 103
    android["trace"]["events"].append({"kind": "finish_lookup", "batch": starts[-1]["call"],
                                       "thread": 1, "sequence": 102, "nonnull": True})
    linux["targets"] = [row for row in linux["targets"] if row["name"] != "finish_b"]
    finish = next(row for row in linux["targets"] if row["name"] == "finish_a")
    finish.update(scope_start="finish_a", context_slot_b60=True)
    linux["targets"].append({"name": "finish_lookup", "module": "libg.so", "offset": 0x1207ffc,
                             "calls": 1, "scope": "finish_a", "scope_start": None,
                             "armed": True, "site_only": False})
    linux["events"] = [row for row in linux["events"] if row["name"] != "finish_b"]
    linux["events"][-1]["context_slot_b60_nonnull"] = True
    linux["events"].append({"name": "finish_lookup", "thread": [1, 2, 3], "return_x0": 0})
    return android, linux


def test_detects_different_lookup_return():
    android, linux = fixture()
    result = compare(android, linux)
    assert result["android_context_nonnull"] and result["linux_context_nonnull"]
    assert result["android_lookup_nonnull"] and not result["linux_lookup_nonnull"]


def test_rejects_missing_return_or_scope():
    android, linux = fixture()
    del linux["events"][-1]["return_x0"]
    with pytest.raises(ValueError, match="missing"):
        compare(android, linux)
    android, linux = fixture()
    changed = copy.deepcopy(android)
    next(row for row in changed["trace"]["events"] if row["kind"] == "finish_lookup")["batch"] = -1
    with pytest.raises(ValueError, match="scope"):
        compare(changed, linux)
