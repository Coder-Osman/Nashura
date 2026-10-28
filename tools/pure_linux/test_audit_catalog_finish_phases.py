import copy

import pytest

from audit_catalog_finish_phases import TARGETS, compare_phases
from test_audit_catalog_batch_counts import fixture as batch_fixture


def fixture():
    android, linux, _ = batch_fixture()
    starts = [row for row in android["trace"]["events"] if row["kind"] == "table_batch"]
    ends = [row for row in android["trace"]["events"] if row["kind"] == "table_batch_end"]
    starts[-1]["sequence"], ends[-1]["sequence"] = 100, 110
    before = copy.deepcopy(linux["events"][-1]["table_counts_after"])
    after_android = copy.deepcopy(ends[-1]["tableCounts"])
    android["trace"]["events"].extend([
        {"kind": "batch_phase", "phase": phase, "edge": edge, "batch": starts[-1]["call"],
         "thread": 1, "sequence": 101 + index, "tableCounts": copy.deepcopy(counts)}
        for index, (phase, edge, counts) in enumerate([
            ("finish_a", "before", before), ("finish_a", "after", after_android),
            ("finish_b", "before", after_android), ("finish_b", "after", after_android)])])
    linux["targets"] = [{"name": name, "module": "libg.so", "offset": offset, "calls": calls,
                         "scope": scope, "scope_start": start, "armed": True, "site_only": False,
                         "table_counts_register": "x0"}
                        for name, (offset, calls, scope, start) in TARGETS.items()]
    for row in linux["events"]:
        row["thread"] = [1, 2, 3]
    for phase in ("finish_a", "finish_b"):
        linux["events"].append({"name": phase, "thread": [1, 2, 3], "return_x0": 0,
                                "table_counts_before": copy.deepcopy(before),
                                "table_counts_after": copy.deepcopy(before)})
    return android, linux


def test_detects_first_phase_divergence():
    android, linux = fixture()
    result = compare_phases(android, linux)
    assert result["first_divergence"]["phase"] == "finish_a"
    assert result["first_divergence"]["edge"] == "after"


def test_rejects_missing_return_and_unobserved_changes():
    android, linux = fixture()
    del linux["events"][-1]["return_x0"]
    with pytest.raises(ValueError, match="returns"):
        compare_phases(android, linux)
    android, linux = fixture()
    phase = next(row for row in android["trace"]["events"]
                 if row.get("phase") == "finish_b" and row.get("edge") == "before")
    phase["tableCounts"][0][1] += 1
    with pytest.raises(ValueError, match="between finish phases"):
        compare_phases(android, linux)
