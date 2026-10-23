import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
DEPENDENCY = ROOT.parent / "fair-final-native-queue-audit-v1/audit_queue.py"
DEPENDENCY_SHA = "9aa8ce29e0b222dda505d96262c03e8f1892dc8e7346e7bf181c2e0b9a751d95"
if hashlib.sha256(DEPENDENCY.read_bytes()).hexdigest() != DEPENDENCY_SHA:
    raise RuntimeError("frozen queue auditor identity differs")
SPEC = importlib.util.spec_from_file_location("execution_flags_queue_dependency", DEPENDENCY)
queue_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue_audit)
command_audit = queue_audit.command_audit
AuditError = command_audit.AuditError

CAPTURE = 0xD30A2C
READS = {
    0xD30C68: ("tbnz", "w23, #1, #0xd30c78"),
    0xD30D98: ("tbnz", "w23, #1, #0xd30dd8"),
}
RETIREMENTS = {
    0xD30AB0: ("ldp", "x24, x23, [sp, #0x60]"),
    0xD30E14: ("ldp", "w21, w23, [x29, #-8]"),
}


def record(instruction):
    return dict(address=hex(instruction.address), mnemonic=instruction.mnemonic,
        op_str=instruction.op_str)


def flag_lifetime(instructions):
    start, end = command_audit.EXECUTOR, command_audit.EXECUTOR_END
    normalized = command_audit.normalize_instructions(instructions)
    queue_audit.control_flow(instructions, start, end)
    indexed = {instruction.address: instruction for instruction in instructions}
    if (indexed[CAPTURE].mnemonic, indexed[CAPTURE].op_str) != ("mov", "w23, w2"):
        raise AuditError("executor argument capture differs")
    pending = [CAPTURE + 4]
    visited, reads, retirements = set(), set(), set()
    while pending:
        address = pending.pop()
        if address in visited:
            continue
        visited.add(address)
        instruction = indexed[address]
        read_registers, written_registers = instruction.regs_access()
        read_names = {instruction.reg_name(register) for register in read_registers}
        written_names = {instruction.reg_name(register) for register in written_registers}
        pair = instruction.mnemonic, instruction.op_str
        if read_names & {"w23", "x23"}:
            if READS.get(address) != pair:
                raise AuditError(f"unclassified live argument read at {address:#x}")
            reads.add(address)
        if written_names & {"w23", "x23"}:
            if RETIREMENTS.get(address) != pair:
                raise AuditError(f"unclassified argument retirement at {address:#x}")
            retirements.add(address)
            continue
        successors = command_audit._successors(normalized, (address - start) // 4, start, end)
        if not successors:
            raise AuditError("executor exits before checked argument retirement")
        pending.extend(successors)
    if reads != set(READS) or retirements != set(RETIREMENTS):
        raise AuditError("executor live argument coverage differs")
    return dict(capture=record(indexed[CAPTURE]),
        reads=[record(indexed[address]) for address in sorted(reads)],
        retirements=[record(indexed[address]) for address in sorted(retirements)],
        live_instruction_count=len(visited), observed_bit_indices=[1],
        bit0_used=False, helper_assumption="AAPCS64 callees preserve x23")


def audit(library):
    source = command_audit.audit(library)
    binary = command_audit.lief.parse(str(library))
    queue = queue_audit.inspect_binary(binary)
    instructions = command_audit._disassemble(binary,
        command_audit.EXECUTOR, command_audit.EXECUTOR_END)
    lifetime = flag_lifetime(instructions)
    paths = {}
    for name, value in (("age20_invokes_executor", 2), ("other_branch_executor_flags", 3)):
        sequence = queue["sequences"][name]
        paths[name] = dict(w2=value, w3=0, bit1=(value >> 1) & 1, sequence=sequence)
    queue_instructions = queue_audit.command_audit._disassemble(binary, 0xD2F37C, 0xD2FE78)
    preconditions = queue_audit.require_sequence(queue_instructions, 0xD2F5B8, [
        ("mov", "x0, x27"), ("bl", "#0xd2bbb4"), ("cmp", "w0, w20"),
        ("b.ne", "#0xd2f608")])
    deployment = queue_audit.require_sequence(instructions, 0xD30DD8, [
        ("movi", "v0.2d, #0xffffffffffffffff"), ("mov", "x0, x22"),
        ("stur", "d0, [x29, #-8]"), ("bl", "#0xf2d2a4"),
        ("ldr", "w3, [sp, #0x10]"), ("mov", "w7, w0"),
        ("sub", "x4, x29, #8"), ("mov", "x0, x22"), ("mov", "x1, x26"),
        ("mov", "w2, w25"), ("mov", "x5, x20"), ("mov", "w6, wzr"),
        ("strb", "wzr, [sp]"), ("bl", "#0xf3816c"),
        ("tbz", "w0, #0, #0xd30edc")])
    return dict(schema="fair-final-native-execution-flags.v1", status="passed_limited_scope",
        library=source["library"], dependency_sha256=DEPENDENCY_SHA,
        executor=hex(command_audit.EXECUTOR), flag_lifetime=lifetime, queue_paths=paths,
        bit0_set_exact_tick_precondition=preconditions, deployment_result_branch=deployment,
        conclusions=dict(w2_2_vs_3_equal_observed_bits=True,
            explicit_bit1_choices_match=True, whole_executor_equivalence_proven=False,
            native_rejection_reachability_proven=False, legal_mask_equivalence_proven=False),
        limitations=[
            "Only explicit reads of the saved w2 argument are covered by the lifetime analysis.",
            "Queue timing and preceding side effects differ; whole queue paths are not equivalent.",
            "Calls are opaque and assumed to obey AAPCS64 callee-saved register rules.",
            "Original w2 or aliases may reach opaque helpers; full argument noninterference is unproved.",
            "Nonzero validation results and deployment failure remain possible syntactically.",
            "No semantic error names, native rejection witness or global unreachability proof.",
        ], training_admissible=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.library)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output.exists() and args.output.read_text() != encoded:
        raise AuditError("existing flags evidence differs")
    args.output.write_text(encoded)
    print(json.dumps({name: report[name] for name in
        ("status", "conclusions", "training_admissible", "gates_closed")}))


if __name__ == "__main__":
    main()
