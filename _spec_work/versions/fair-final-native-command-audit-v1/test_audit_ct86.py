from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).with_name("audit_ct86.py")
SPEC = importlib.util.spec_from_file_location("audit_ct86", MODULE_PATH)
audit_ct86 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = audit_ct86
SPEC.loader.exec_module(audit_ct86)
Op = audit_ct86.Op


def test_conditional_paths_keep_distinct_return_values():
    ops = [
        Op(0x1000, "cbz", target=0x100C),
        Op(0x1004, "mov", "w0, #3", writes_result=True, result_kind="literal", result_value=3),
        Op(0x1008, "b", target=0x1010),
        Op(0x100C, "mov", "w0, #4", writes_result=True, result_kind="literal", result_value=4),
        Op(0x1010, "ret"),
    ]
    report = audit_ct86.analyze_cfg(ops, 0x1000, 0x1014)
    observed = {definition["value"] for ret in report["returns"] for definition in ret["definitions"]}
    assert observed == {3, 4}


def test_call_clobber_is_reported_not_misread_as_previous_literal():
    ops = [
        Op(0x2000, "mov", "w0, #3", writes_result=True, result_kind="literal", result_value=3),
        Op(0x2004, "bl", "#0x3000", target=0x3000, writes_result=True,
           result_kind="call-result-unknown"),
        Op(0x2008, "ret"),
    ]
    report = audit_ct86.analyze_cfg(ops, 0x2000, 0x200C)
    definitions = report["returns"][0]["definitions"]
    assert [(item["kind"], item["value"]) for item in definitions] == [("call-result-unknown", None)]


def test_dynamic_arithmetic_return_is_preserved():
    ops = [
        Op(0x3000, "bl", "#0x4000", target=0x4000, writes_result=True,
           result_kind="call-result-unknown"),
        Op(0x3004, "add", "w0, w0, #0xe", writes_result=True,
           result_kind="arithmetic-derived", result_value=14),
        Op(0x3008, "ret"),
    ]
    report = audit_ct86.analyze_cfg(ops, 0x3000, 0x300C)
    assert report["returns"][0]["definitions"] == [
        {"address": "0x3004", "kind": "arithmetic-derived", "value": 14}
    ]


def test_loop_reaches_fixed_point():
    ops = [
        Op(0x4000, "mov", "w0, #1", writes_result=True, result_kind="literal", result_value=1),
        Op(0x4004, "cbnz", target=0x4004),
        Op(0x4008, "ret"),
    ]
    report = audit_ct86.analyze_cfg(ops, 0x4000, 0x400C)
    assert report["returns"][0]["definitions"][0]["value"] == 1


@pytest.mark.parametrize("ops,start,end", [
    ([Op(0x5000, "br")], 0x5000, 0x5004),
    ([Op(0x6000, "b", target=0x7000)], 0x6000, 0x6004),
    ([Op(0x8000, "nop")], 0x8000, 0x8004),
])
def test_unsafe_or_incomplete_control_flow_fails_closed(ops, start, end):
    with pytest.raises(audit_ct86.AuditError):
        audit_ct86.analyze_cfg(ops, start, end)


def test_unclassified_return_definition_fails_closed():
    ops = [
        Op(0x9000, "mov", "w0, w1", writes_result=True, result_kind="opaque-local-definition"),
        Op(0x9004, "ret"),
    ]
    with pytest.raises(audit_ct86.AuditError, match="unclassified return"):
        audit_ct86.analyze_cfg(ops, 0x9000, 0x9008)


def test_frozen_library_audit_checks_all_local_returns():
    library = (Path(__file__).parents[1] / "fair-final-history-v2" / "project" / "artifacts" /
               "standalone-engine-20260926" / "app-libs" / "libg.so")
    if not library.exists():
        pytest.skip("frozen engine resource not present")
    report = audit_ct86.audit(library)
    assert report["status"] == "passed_limited_scope"
    assert report["command_mapping"]["target"] == "0xd2e63c"
    assert report["command_mapping"]["executor"] == "0xd30a00"
    assert report["executor_local_cfg"]["instruction_count"] == (0xD30F2C - 0xD30A00) // 4
    assert report["executor_local_cfg"]["reachable_instruction_count"] > 100
    assert report["return_code_observations"]
    assert report["dynamic_return_definitions"]
    assert report["training_admissible"] is False


def test_unexpected_binary_sha_is_rejected(tmp_path):
    copy = tmp_path / "altered.so"
    copy.write_bytes(b"not the frozen engine")
    with pytest.raises(audit_ct86.AuditError, match="unexpected library SHA"):
        audit_ct86.audit(copy)
