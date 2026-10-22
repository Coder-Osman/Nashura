from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import lief


DEPENDENCY_PATH = Path(__file__).parents[1] / "fair-final-native-command-audit-v1" / "audit_ct86.py"
DEPENDENCY_SHA = "e70ce714bd8d6a266c8a0990ad5ebe63e8a40edb5e0a957e4d3c4292c6c84fe5"
if hashlib.sha256(DEPENDENCY_PATH.read_bytes()).hexdigest() != DEPENDENCY_SHA:
    raise RuntimeError("frozen command auditor identity differs")
MODULE_SPEC = importlib.util.spec_from_file_location("queue_dependency_ct86", DEPENDENCY_PATH)
command_audit = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = command_audit
MODULE_SPEC.loader.exec_module(command_audit)
AuditError = command_audit.AuditError


RANGES = {
    "manager_step": (0x10620A4, 0x1062274, ()),
    "manager_inner": (0x1062274, 0x10625DC, (0xD2F37C,)),
    "queue_append": (0xD2EEF0, 0xD2EFB4, ()),
    "queue_step": (0xD2F37C, 0xD2FE78, ()),
    "command_age": (0xD2BC94, 0xD2BCA0, ()),
    "command_type": (0xD31204, 0xD3120C, ()),
    "manager_tick": (0x1062BC0, 0x1062BC8, ()),
    "command_tick_setter": (0xD2BC84, 0xD2BC8C, ()),
}

INDIRECT_TAIL_SEQUENCES = {
    0x10623B8: (0x1062398, [
        ("ldr", "x0, [x19, #0xb0]"), ("cbz", "x0, #0x10623bc"),
        ("ldr", "x8, [x0]"), ("ldr", "w1, [x19, #0x60]"),
        ("ldr", "x2, [x8, #0x4b8]"), ("ldp", "x20, x19, [sp, #0x20]"),
        ("ldp", "x22, x21, [sp, #0x10]"), ("ldp", "x29, x30, [sp], #0x30"),
        ("br", "x2")]),
}

SEQUENCES = {
    "manager_step_calls_inner": (0x1062140, [
        ("mov", "x0, x19"), ("bl", "#0x1062274")]),
    "manager_calls_queue": (0x1062374, [
        ("ldr", "x0, [x19, #0x38]"), ("bl", "#0xd2f37c")]),
    "manager_tail_calls_queue": (0x10625C8, [
        ("ldr", "x0, [x19, #0x38]"), ("ldp", "x20, x19, [sp, #0x20]"),
        ("ldp", "x22, x21, [sp, #0x10]"), ("ldp", "x29, x30, [sp], #0x30"),
        ("b", "#0xd2f37c")]),
    "queue_branch_predicate": (0xD2F39C, [
        ("mov", "x19, x0"), ("ldr", "x0, [x0, #0x20]"), ("bl", "#0x1062f6c"),
        ("ldr", "x8, [x19, #0x20]"), ("mov", "w21, w0"), ("mov", "x0, x8"),
        ("bl", "#0x1062bc0"), ("mov", "w20, w0"), ("tbz", "w21, #0, #0xd2f8bc")]),
    "age_reads_command_tick": (0xD2BC94, [
        ("ldr", "w8, [x0, #0x10]"), ("sub", "w0, w1, w8"), ("ret", "")]),
    "age_recomputed_after_old_tick_adjustment": (0xD2F908, [
        ("mov", "x0, x26"), ("mov", "w1, w20"), ("bl", "#0xd2bc94"),
        ("cmp", "w0, #0x15"), ("b.lt", "#0xd2f944"),
        ("mov", "x0, x26"), ("bl", "#0xd2bc8c"),
        ("ldr", "x8, [x26]"), ("mov", "x0, x26"), ("mov", "w1, w23"),
        ("ldr", "x8, [x8, #0x40]"), ("blr", "x8"),
        ("mov", "x0, x26"), ("mov", "w1, w20"), ("bl", "#0xd2bc94")]),
    "age_branch": (0xD2F944, [
        ("cmp", "w0, #0x14"), ("b.ne", "#0xd2fa18")]),
    "younger_queued_older_removed": (0xD2FA18, [
        ("cmp", "w0, #0x13"), ("b.gt", "#0xd2fa28"),
        ("add", "w28, w28, #1"), ("b", "#0xd2f8ec")]),
    "age20_invokes_executor": (0xD2FAC0, [
        ("ldr", "x8, [x26]"), ("strb", "wzr, [x26, #8]"), ("mov", "x0, x26"),
        ("ldr", "x1, [x19, #0x20]"), ("mov", "w2, #2"), ("mov", "w3, wzr"),
        ("ldr", "x8, [x8, #0x18]"), ("blr", "x8"),
        ("cbz", "w0, #0xd2faec"), ("strb", "w21, [x26, #8]"), ("b", "#0xd2fb20")]),
    "success_attests_tick": (0xD2FAEC, [
        ("mov", "x0, x26"), ("mov", "w1, w20"), ("bl", "#0xd2bbac")]),
    "both_results_destroy_command": (0xD2FB20, [
        ("ldr", "x8, [x26]"), ("mov", "x0, x26"), ("sub", "w27, w27, #1"),
        ("ldr", "x8, [x8]"), ("blr", "x8"), ("ldr", "x8, [x26]"),
        ("mov", "x0, x26"), ("ldr", "x8, [x8, #0x10]"), ("blr", "x8"),
        ("b", "#0xd2f8ec")]),
    "other_branch_executor_flags": (0xD2F820, [
        ("ldr", "x8, [x28]"), ("ldr", "x1, [x19, #0x20]"), ("mov", "x0, x28"),
        ("mov", "w2, #3"), ("mov", "w3, wzr"),
        ("ldr", "x8, [x8, #0x18]"), ("blr", "x8")]),
    "type_getter": (0xD31204, [("mov", "w0, #0x56"), ("ret", "")]),
    "manager_current_tick": (0x1062BC0, [("ldr", "w0, [x0, #0x60]"), ("ret", "")]),
    "command_tick_write": (0xD2BC84, [("str", "w1, [x0, #0x10]"), ("ret", "")]),
}


def require_sequence(instructions, start, expected):
    indexed = {instruction.address: instruction for instruction in instructions}
    observed = []
    for offset, instruction in enumerate(expected):
        address = start + 4 * offset
        actual = indexed.get(address)
        if actual is None or (actual.mnemonic, actual.op_str) != instruction:
            raise AuditError(f"sequence differs at {address:#x}")
        observed.append({"address": hex(address), "instruction": " ".join(instruction).rstrip()})
    return observed


def control_flow(instructions, start, end, tail_targets=(), indirect_tails=None):
    normalized = command_audit.normalize_instructions(instructions)
    if not normalized or end != start + len(normalized) * 4 or any(
            instruction.address != start + index * 4 for index, instruction in enumerate(normalized)):
        raise AuditError("instruction sequence does not cover exact contiguous range")
    indirect_tails = {} if indirect_tails is None else indirect_tails
    for address, (sequence_start, expected) in indirect_tails.items():
        if sequence_start + (len(expected) - 1) * 4 != address or expected[-1][0] != "br":
            raise AuditError("invalid indirect tail registration")
        require_sequence(instructions, sequence_start, expected)
    indexed = {instruction.address: instruction for instruction in normalized}
    pending = [start]
    reachable = set()
    successors = {}
    exits = []
    while pending:
        address = pending.pop()
        if address in reachable:
            continue
        if address not in indexed:
            raise AuditError(f"uncovered control flow at {address:#x}")
        reachable.add(address)
        instruction = indexed[address]
        if instruction.mnemonic == "b" and instruction.target in tail_targets:
            exits.append({"address": hex(address), "tail_target": hex(instruction.target)})
            targets = []
        elif instruction.mnemonic == "br" and address in indirect_tails:
            exits.append({"address": hex(address), "indirect_tail_register": instruction.op_str,
                          "target_resolution": "unresolved virtual tail call"})
            targets = []
        else:
            targets = command_audit._successors(normalized, (address - start) // 4, start, end)
        successors[hex(address)] = [hex(target) for target in targets]
        pending.extend(targets)
    return {
        "start": hex(start), "end_exclusive": hex(end),
        "instruction_count": len(instructions), "reachable_instruction_count": len(reachable),
        "successors": successors, "tail_exits": exits,
        "returns": [hex(address) for address in sorted(reachable)
                    if indexed[address].mnemonic in {"ret", "retaa", "retab"}],
        "disassembly": [{"address": hex(instruction.address),
                         "instruction": f"{instruction.mnemonic} {instruction.op_str}".rstrip()}
                        for instruction in instructions],
    }


def inspect_binary(binary):
    decoded = {}
    flows = {}
    for name, (start, end, tail_targets) in RANGES.items():
        instructions = command_audit._disassemble(binary, start, end)
        decoded[name] = instructions
        indirect_tails = INDIRECT_TAIL_SEQUENCES if name == "manager_inner" else None
        flows[name] = control_flow(instructions, start, end, tail_targets, indirect_tails)
    instructions = [instruction for group in decoded.values() for instruction in group]
    sequences = {name: require_sequence(instructions, start, expected)
                 for name, (start, expected) in SEQUENCES.items()}
    relocations = {int(item.address): int(item.addend) for item in binary.relocations}
    expected_slots = {0x18776B8: 0xD30A00, 0x18776C0: 0xD31204, 0x18776E0: 0xD2BC84}
    if any(relocations.get(slot) != target for slot, target in expected_slots.items()):
        raise AuditError("ct86 executor/type/tick-setter vtable identity differs")
    for name, sequence in sequences.items():
        address = int(sequence[0]["address"], 16)
        if not any(hex(address) in flow["successors"] for flow in flows.values()):
            raise AuditError(f"anchor not conservatively reachable: {name}")
    return {"ranges": flows, "sequences": sequences,
            "vtable_slots": {hex(slot): hex(target) for slot, target in expected_slots.items()}}


def audit(library):
    source = command_audit.audit(library)
    binary = lief.parse(str(library))
    inspection = inspect_binary(binary)
    return {
        "schema": "fair-final-native-queue-static-audit.v1", "status": "passed_limited_scope",
        "training_admissible": False, "gates_closed": False,
        "library": source["library"], "dependency_sha256": DEPENDENCY_SHA,
        "ct86_executor": source["command_mapping"], **inspection,
        "observations": {
            "manager_queue_offset": "0x38", "executor_slot": "0x18",
        "age20_branch_arguments": {"w2": 2, "w3": 0},
            "other_branch_arguments": {"w2": 3, "w3": 0},
            "age20_nonzero_result": "sets command+8 then joins destruction path",
            "age20_zero_result": "writes execution tick then joins destruction path",
            "age_comparison": 20, "command_type": 86,
            "age_note": "Age is recomputed after a virtual tick adjustment for initial age >=21.",
        },
        "limitations": [
            "Conservative static CFG; branch feasibility and runtime mode predicate remain unproved.",
            "One manager virtual tail exit is explicitly fingerprinted; its destination is unresolved.",
            "The native live queue can call the ct86 validation slot; no legal rejection is demonstrated.",
            "No semantic names for nonzero errors, mask equivalence, or pending reachability are claimed.",
            "No dynamic hook, production source change, native experiment, or new gate PASS is implied.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = audit(arguments.library)
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    if arguments.output.exists() and arguments.output.read_text() != encoded:
        raise AuditError("existing audit output differs")
    arguments.output.write_text(encoded)
    print(json.dumps({key: report[key] for key in ("status", "gates_closed", "observations")}))


if __name__ == "__main__":
    main()
