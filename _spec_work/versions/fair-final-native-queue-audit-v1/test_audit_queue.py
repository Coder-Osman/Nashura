import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import lief
import pytest


SPEC = importlib.util.spec_from_file_location("audit_queue", Path(__file__).with_name("audit_queue.py"))
audit_queue = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_queue
SPEC.loader.exec_module(audit_queue)
LIBRARY = (Path(__file__).parents[1] / "fair-final-history-v2" / "project" / "artifacts" /
           "standalone-engine-20260926" / "app-libs" / "libg.so")


@pytest.fixture(scope="module")
def binary():
    return lief.parse(str(LIBRARY))


def test_frozen_native_queue_chain(binary):
    report = audit_queue.audit(LIBRARY)
    assert report["status"] == "passed_limited_scope"
    assert report["observations"]["age20_branch_arguments"] == {"w2": 2, "w3": 0}
    assert report["observations"]["other_branch_arguments"] == {"w2": 3, "w3": 0}
    assert report["ct86_executor"]["executor"] == "0xd30a00"
    assert report["gates_closed"] is False
    assert report["training_admissible"] is False
    assert report["ranges"]["queue_step"]["reachable_instruction_count"] > 500
    assert {item["address"] for item in report["ranges"]["manager_inner"]["tail_exits"]} == {
        "0x10625d8", "0x10623b8"}


@pytest.mark.parametrize("name", sorted(audit_queue.SEQUENCES))
def test_each_semantic_anchor_rejects_changed_instruction(binary, name):
    start, expected = audit_queue.SEQUENCES[name]
    instructions = list(audit_queue.command_audit._disassemble(binary, start, start + 4 * len(expected)))
    instructions[-1] = SimpleNamespace(address=instructions[-1].address, mnemonic="nop", op_str="")
    with pytest.raises(audit_queue.AuditError, match="sequence differs"):
        audit_queue.require_sequence(instructions, start, expected)


def test_missing_anchor_fails_closed():
    with pytest.raises(audit_queue.AuditError, match="sequence differs"):
        audit_queue.require_sequence([], 0x1000, [("ret", "")])


def test_wrong_library_is_rejected(tmp_path):
    library = tmp_path / "wrong.so"
    library.write_bytes(b"wrong identity")
    with pytest.raises(audit_queue.AuditError, match="unexpected library SHA"):
        audit_queue.audit(library)


def test_changed_vtable_target_is_rejected(binary):
    changed = False
    for relocation in binary.relocations:
        if relocation.address == 0x18776B8:
            original = relocation.addend
            try:
                relocation.addend = original + 4
                with pytest.raises(audit_queue.AuditError, match="vtable identity differs"):
                    audit_queue.inspect_binary(binary)
            finally:
                relocation.addend = original
            changed = True
            break
    assert changed


def test_external_tail_branch_requires_explicit_registration(binary):
    instructions = audit_queue.command_audit._disassemble(binary, 0x1062274, 0x10625DC)
    with pytest.raises(audit_queue.AuditError, match="outside/alignment-invalid"):
        audit_queue.control_flow(instructions, 0x1062274, 0x10625DC,
                                 indirect_tails=audit_queue.INDIRECT_TAIL_SEQUENCES)


def test_virtual_tail_requires_registration(binary):
    instructions = audit_queue.command_audit._disassemble(binary, 0x1062274, 0x10625DC)
    with pytest.raises(audit_queue.AuditError, match="indirect control transfer"):
        audit_queue.control_flow(instructions, 0x1062274, 0x10625DC, (0xD2F37C,))


@pytest.mark.parametrize("offset", range(9))
def test_virtual_tail_fingerprint_rejects_each_mutation(binary, offset):
    instructions = list(audit_queue.command_audit._disassemble(binary, 0x1062274, 0x10625DC))
    position = (0x1062398 - 0x1062274) // 4 + offset
    original = instructions[position]
    instructions[position] = SimpleNamespace(address=original.address, mnemonic="nop", op_str="")
    start, expected = audit_queue.INDIRECT_TAIL_SEQUENCES[0x10623B8]
    with pytest.raises(audit_queue.AuditError, match="sequence differs"):
        audit_queue.require_sequence(instructions, start, expected)


def test_missing_instruction_rejected_before_traversal(binary):
    instructions = list(audit_queue.command_audit._disassemble(binary, 0xD31204, 0xD3120C))
    with pytest.raises(audit_queue.AuditError, match="exact contiguous range"):
        audit_queue.control_flow(instructions[:-1], 0xD31204, 0xD3120C)


def test_reachable_indirect_jump_fails_closed(binary):
    instructions = audit_queue.command_audit._disassemble(binary, 0xD2F0AC, 0xD2F0C0)
    with pytest.raises(audit_queue.AuditError, match="indirect control transfer"):
        audit_queue.control_flow(instructions, 0xD2F0AC, 0xD2F0C0)
