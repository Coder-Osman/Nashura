import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from callsite_audit import build_report, canonical, object_kind, slice_arguments


def instruction(address, mnemonic, operands, writes=()):
    return SimpleNamespace(address=address, size=4, mnemonic=mnemonic, op_str=operands,
        regs_access=lambda: ([], list(writes)), reg_name=lambda register: register)


def initialize(address):
    return instruction(address, "bl", "#0x121b118")


def test_stack_object_and_w_seed_alias_are_distinct_from_x_value():
    result = slice_arguments([instruction(0x100, "mov", "w19, #7", ["w19"]),
        instruction(0x104, "add", "x0, sp, #0x40", ["x0"]),
        instruction(0x108, "mov", "w1, w19", ["w1"]), initialize(0x10C)])
    assert object_kind(result["object"]) == "stack_relative"
    seed = result["seed"]["value"]["value"]["value"]
    assert seed == {"kind": "zero_extend32", "value": {"kind": "constant", "value": 7}}


def test_unoffset_stack_argument_is_still_stack_relative():
    result = slice_arguments([instruction(0x100, "mov", "x0, sp", ["x0"]), initialize(0x104)])
    assert object_kind(result["object"]) == "stack_relative"


def test_call_clobbers_temporaries_and_preserves_saved_registers():
    result = slice_arguments([instruction(0x100, "mov", "x19, x0", ["x19"]),
        instruction(0x104, "mov", "x8, x0", ["x8"]),
        instruction(0x108, "mov", "x18, x0", ["x18"]),
        instruction(0x10C, "bl", "#0x1000"),
        instruction(0x110, "mov", "x0, x19", ["x0"]),
        instruction(0x114, "add", "w1, w8, w18", ["w1"]), initialize(0x118)])
    assert result["object"] == {"kind": "live_in", "register": "x0", "at": "0x100"}
    expression = result["seed"]["value"]["value"]
    assert expression["left"]["value"]["kind"] == "call_clobbered"
    assert expression["right"]["value"]["register"] == "x18"


def test_allocation_return_is_not_a_lifetime_or_reachability_claim():
    result = slice_arguments([instruction(0x100, "bl", "#0x17e4b20"),
        instruction(0x104, "mov", "x23, x0", ["x23"]),
        instruction(0x108, "bl", "#0x900"),
        instruction(0x10C, "mov", "x0, x23", ["x0"]), initialize(0x110)])
    assert object_kind(result["object"]) == "allocation_return"
    assert result["seed"]["value"]["kind"] == "call_clobbered"


def test_opaque_w_write_invalidates_the_entire_x_alias():
    result = slice_arguments([instruction(0x100, "mov", "x19, x0", ["x19"]),
        instruction(0x104, "ldr", "w19, [sp]", ["w19"]),
        instruction(0x108, "mov", "x0, x19", ["x0"]), initialize(0x10C)])
    assert object_kind(result["object"]) == "unresolved"
    assert result["object"]["value"]["kind"] == "opaque_write"


def test_zero_register_is_a_constant_and_sp_adjustment_is_explicit():
    result = slice_arguments([instruction(0x100, "sub", "x0, sp, #0x20", ["x0"]),
        instruction(0x104, "mov", "w1, wzr", ["w1"]), initialize(0x108)])
    assert object_kind(result["object"]) == "stack_relative"
    assert result["object"]["kind"] == "sub"
    assert result["seed"]["value"]["value"]["value"]["value"] == 0


def test_interior_direct_call_without_a_concrete_target_is_rejected():
    with pytest.raises(ValueError, match="unresolved direct call"):
        slice_arguments([instruction(0x100, "bl", "x8"), initialize(0x104)])


@pytest.mark.parametrize("mnemonic,operands", [("b", "#0x108"), ("b.eq", "#0x108"),
    ("cbz", "x0, #0x108"), ("tbz", "w0, #0, #0x108"), ("br", "x8"),
    ("blr", "x8"), ("ret", ""), ("svc", "#0")])
def test_control_transfers_are_rejected_instead_of_assuming_fallthrough(mnemonic, operands):
    with pytest.raises(ValueError, match="control transfer"):
        slice_arguments([instruction(0x100, mnemonic, operands), initialize(0x104)])


@pytest.mark.parametrize("instructions", [[], [initialize(0x100), initialize(0x108)],
    [instruction(0x100, "bl", "#0x900")]])
def test_empty_gapped_and_wrong_final_calls_are_rejected(instructions):
    with pytest.raises(ValueError):
        slice_arguments(instructions)


@pytest.mark.parametrize("register", ["v0", "x31", "w32", "fp", ""])
def test_unknown_register_spellings_are_rejected(register):
    with pytest.raises(ValueError):
        canonical(register)


def test_wrong_library_identity_fails_before_parsing(tmp_path):
    library = tmp_path / "changed.so"
    library.write_bytes(b"different library")
    with pytest.raises(ValueError, match="identity"):
        build_report(library)


def test_fixed_native_library_census_and_argument_classification():
    library = Path(__file__).resolve().parent.parent / (
        "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so")
    if not library.exists():
        pytest.skip("private fixed native library is not distributed")
    report = build_report(library)
    assert report == json.loads((Path(__file__).parent / "static-report.json").read_text())
    assert report["direct_initializer_count"] == 18
    assert report["counts"] == {"incoming": 1, "stack_relative": 11, "allocation_return": 6}
    assert report["allocation_import"]["name"] == "_Znwm"
    assert len({record["callsite"] for record in report["records"]}) == 18
    for record in report["records"]:
        assert record["snapshot_coverage_proven"] is False
        assert record["fixed_hog_reachable_proven"] is False
        assert record["escapes_or_callee_retention_proven"] is False
    assert report["hidden_random_coverage_closed"] is False
    assert report["training_admissible"] is False
    assert report["gates_closed"] is False
