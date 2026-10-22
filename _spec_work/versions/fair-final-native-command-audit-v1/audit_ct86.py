from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

import capstone
import lief


EXPECTED_SHA256 = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
FACTORY_START = 0xD2C138
FACTORY_TABLE = 0x341B38
FACTORY_DISPATCH = 0xD2C174
COMMAND_ID = 86
COMMAND_ENTRY = 0xD2E63C
CONSTRUCTOR = 0xD309A8
VTABLE = 0x18776A0
EXECUTOR = 0xD30A00
EXECUTOR_END = 0xD30F2C
POSITION_HELPER = 0xCFCF50
POSITION_HELPER_END = 0xCFD094


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class Op:
    address: int
    mnemonic: str
    op_str: str = ""
    target: int | None = None
    writes_result: bool = False
    result_kind: str | None = None
    result_value: int | None = None


@dataclass(frozen=True)
class ResultDef:
    address: int
    kind: str
    value: int | None = None


def _expected_file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _segment_covers(binary: lief.Binary, address: int, size: int, executable: bool = False) -> bool:
    for segment in binary.segments:
        start = int(segment.virtual_address)
        end = start + int(segment.virtual_size)
        if start <= address and address + size <= end:
            if not executable or bool(segment.flags & lief.ELF.Segment.FLAGS.X):
                return True
    return False


def _bytes_at(binary: lief.Binary, address: int, size: int) -> bytes:
    try:
        value = bytes(binary.get_content_from_virtual_address(address, size))
    except Exception as exc:  # LIEF reports malformed and out-of-segment access differently by version.
        raise AuditError(f"cannot read virtual range {address:#x}+{size:#x}") from exc
    if len(value) != size:
        raise AuditError(f"short virtual read at {address:#x}: {len(value)} != {size}")
    return value


def _disassemble(binary: lief.Binary, start: int, end: int) -> list[capstone.CsInsn]:
    size = end - start
    if size <= 0 or size % 4:
        raise AuditError(f"invalid AArch64 range {start:#x}..{end:#x}")
    if not _segment_covers(binary, start, size, executable=True):
        raise AuditError(f"range {start:#x}..{end:#x} is not covered by executable segment")
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    decoder.detail = True
    code = _bytes_at(binary, start, size)
    instructions = list(decoder.disasm(code, start))
    covered = sum(item.size for item in instructions)
    if covered != size or any(item.size != 4 for item in instructions):
        raise AuditError(f"incomplete AArch64 decode at {start:#x}: {covered}/{size} bytes")
    for expected, instruction in enumerate(instructions):
        if instruction.address != start + expected * 4:
            raise AuditError(f"instruction gap at {start + expected * 4:#x}")
    return instructions


def _result_effect(insn: capstone.CsInsn) -> tuple[bool, str | None, int | None]:
    if insn.mnemonic in {"bl", "blr"} or insn.mnemonic.startswith("bl."):
        return True, "call-result-unknown", None
    _, writes = insn.regs_access()
    written = {insn.reg_name(reg) for reg in writes}
    writes_result = bool(written & {"w0", "x0"})
    if not writes_result:
        return False, None, None
    operands = insn.operands
    if insn.mnemonic == "mov" and len(operands) >= 2:
        source = operands[1]
        if source.type == capstone.arm64.ARM64_OP_IMM:
            return True, "literal", int(source.imm)
        if source.type == capstone.arm64.ARM64_OP_REG and insn.reg_name(source.reg) in {"wzr", "xzr"}:
            return True, "literal", 0
    if insn.mnemonic == "add" and len(operands) >= 3:
        destination = insn.reg_name(operands[0].reg) if operands[0].type == capstone.arm64.ARM64_OP_REG else ""
        source = insn.reg_name(operands[1].reg) if operands[1].type == capstone.arm64.ARM64_OP_REG else ""
        immediate = operands[2].imm if operands[2].type == capstone.arm64.ARM64_OP_IMM else None
        if destination in {"w0", "x0"} and source in {"w0", "x0"} and immediate is not None:
            return True, "arithmetic-derived", int(immediate)
    return True, "opaque-local-definition", None


def normalize_instructions(instructions: Iterable[capstone.CsInsn]) -> list[Op]:
    normalized: list[Op] = []
    for insn in instructions:
        mnemonic = insn.mnemonic.lower()
        target = None
        if insn.operands and insn.operands[-1].type == capstone.arm64.ARM64_OP_IMM:
            target = int(insn.operands[-1].imm)
        writes_result, result_kind, result_value = _result_effect(insn)
        normalized.append(Op(insn.address, mnemonic, insn.op_str, target,
                             writes_result, result_kind, result_value))
    return normalized


def _is_conditional_branch(mnemonic: str) -> bool:
    return mnemonic.startswith(("b.", "cbz", "cbnz", "tbz", "tbnz"))


def _successors(ops: list[Op], index: int, start: int, end: int) -> list[int]:
    op = ops[index]
    next_address = op.address + 4
    mnemonic = op.mnemonic
    if mnemonic in {"ret", "retaa", "retab"}:
        return []
    if mnemonic == "br":
        raise AuditError(f"indirect control transfer at {op.address:#x}")
    if mnemonic == "b":
        targets = [op.target]
    elif _is_conditional_branch(mnemonic):
        targets = [op.target, next_address]
    elif mnemonic in {"bl", "blr"} or mnemonic.startswith("bl."):
        targets = [next_address]
    else:
        targets = [next_address]
    result: list[int] = []
    for target in targets:
        if target is None:
            raise AuditError(f"branch without direct destination at {op.address:#x}")
        if target == end:
            raise AuditError(f"control flow exits audited range at {op.address:#x}")
        if target < start or target >= end or target % 4:
            raise AuditError(f"branch target {target:#x} outside/alignment-invalid for audited range")
        result.append(target)
    return result


def analyze_cfg(ops: list[Op], start: int, end: int) -> dict:
    if not ops or ops[0].address != start:
        raise AuditError("instruction sequence does not start at requested entry")
    if any(op.address != start + index * 4 for index, op in enumerate(ops)):
        raise AuditError("instruction sequence contains gaps or overlaps")
    if end != start + len(ops) * 4:
        raise AuditError("instruction sequence does not cover exact requested range")
    by_address = {op.address: op for op in ops}
    successors: dict[int, list[int]] = {}
    for index, op in enumerate(ops):
        successors[op.address] = _successors(ops, index, start, end)

    initial = ResultDef(start, "entry-value-unknown")
    incoming: dict[int, set[ResultDef]] = {start: {initial}}
    queue = [start]
    queued = {start}
    reachable: set[int] = set()
    returns: dict[int, set[ResultDef]] = {}
    while queue:
        address = queue.pop(0)
        queued.discard(address)
        reachable.add(address)
        op = by_address[address]
        state = set(incoming[address])
        if op.writes_result:
            if op.result_kind is None:
                raise AuditError(f"unclassified result definition at {address:#x}")
            state = {ResultDef(address, op.result_kind, op.result_value)}
        if op.mnemonic in {"ret", "retaa", "retab"}:
            returns[address] = state
        for successor in successors[address]:
            merged = incoming.setdefault(successor, set()) | state
            if merged != incoming[successor]:
                incoming[successor] = merged
                if successor not in queued:
                    queue.append(successor)
                    queued.add(successor)

    if not returns:
        raise AuditError("reachable CFG contains no return")
    for address, definitions in returns.items():
        if not definitions or any(item.kind in {"entry-value-unknown", "opaque-local-definition"}
                                  for item in definitions):
            raise AuditError(f"unclassified return value at {address:#x}: {sorted(definitions, key=repr)}")

    call_sites = [op for op in ops if op.mnemonic in {"bl", "blr"} or op.mnemonic.startswith("bl.")]
    indirect_calls = [op for op in call_sites if op.mnemonic == "blr" or op.mnemonic.startswith("bl.")]
    return {
        "instruction_count": len(ops),
        "reachable_instruction_count": len(reachable),
        "reachable_addresses": sorted(reachable),
        "successors": {hex(address): [hex(item) for item in successors[address]] for address in sorted(reachable)},
        "direct_calls": [{"address": hex(op.address), "target": hex(op.target) if op.target is not None else None,
                          "instruction": f"{op.mnemonic} {op.op_str}".strip()} for op in call_sites if op not in indirect_calls],
        "indirect_calls": [{"address": hex(op.address), "instruction": f"{op.mnemonic} {op.op_str}".strip()}
                           for op in indirect_calls],
        "returns": [{"address": hex(address), "definitions": [
            {"address": hex(item.address), "kind": item.kind, "value": item.value}
            for item in sorted(definitions, key=lambda value: (value.address, value.kind, value.value or 0))
        ]} for address, definitions in sorted(returns.items())],
        "disassembly": [{"address": hex(op.address), "instruction": f"{op.mnemonic} {op.op_str}".strip()}
                        for op in ops],
    }


def _check_anchor(binary: lief.Binary, address: int, expected: bytes, name: str) -> None:
    actual = _bytes_at(binary, address, len(expected))
    if actual != expected:
        raise AuditError(f"anchor mismatch: {name} at {address:#x}")


def audit(path: Path) -> dict:
    actual_sha = _expected_file_sha(path)
    if actual_sha != EXPECTED_SHA256:
        raise AuditError(f"unexpected library SHA-256: {actual_sha}")
    binary = lief.parse(str(path))
    if binary is None or not str(binary.header.machine_type).endswith("AARCH64"):
        raise AuditError("input is not the expected AArch64 ELF")

    factory_code = _bytes_at(binary, FACTORY_START, 0x3C)
    _check_anchor(binary, FACTORY_START + 0x10, bytes.fromhex("09080051"), "ct-minus-two selector")
    _check_anchor(binary, FACTORY_START + 0x30, bytes.fromhex("0b796978"), "u16 table lookup")
    _check_anchor(binary, FACTORY_START + 0x34, bytes.fromhex("4a090b8b"), "relative branch target calculation")
    _check_anchor(binary, FACTORY_START + 0x38, bytes.fromhex("40011fd6"), "indirect factory dispatch")
    if not _segment_covers(binary, FACTORY_START, len(factory_code), executable=True):
        raise AuditError("factory dispatch is not in executable segment")

    table_index = COMMAND_ID - 2
    entry_address = FACTORY_TABLE + 2 * table_index
    table_word = int.from_bytes(_bytes_at(binary, entry_address, 2), "little")
    factory_target = FACTORY_DISPATCH + 4 * table_word
    if factory_target != COMMAND_ENTRY:
        raise AuditError(f"command {COMMAND_ID} factory target mismatch: {factory_target:#x}")
    command = _disassemble(binary, COMMAND_ENTRY, COMMAND_ENTRY + 0x28)
    command_text = [(item.mnemonic, item.op_str) for item in command]
    expected_command = [
        ("mov", "w0, #0x58"), ("bl", "#0x17e4b20"), ("mov", "x1, x20"),
        ("mov", "x21, x0"), ("bl", "#0xd309a8"),
    ]
    if command_text[:5] != expected_command:
        raise AuditError(f"ct86 entry shape mismatch: {command_text[:5]}")

    ctor = _disassemble(binary, CONSTRUCTOR, CONSTRUCTOR + 0x58)
    vtable_relocations = {int(relocation.address): int(relocation.addend) for relocation in binary.relocations
                          if VTABLE <= int(relocation.address) < VTABLE + 0x80}
    if vtable_relocations.get(VTABLE + 0x18) != EXECUTOR:
        raise AuditError("command executor vtable slot +0x18 does not target local audited routine")
    if not _segment_covers(binary, EXECUTOR, EXECUTOR_END - EXECUTOR, executable=True):
        raise AuditError("executor range is not in executable segment")
    if not _segment_covers(binary, POSITION_HELPER, POSITION_HELPER_END - POSITION_HELPER, executable=True):
        raise AuditError("position helper range is not in executable segment")

    executor_ops = normalize_instructions(_disassemble(binary, EXECUTOR, EXECUTOR_END))
    helper_ops = normalize_instructions(_disassemble(binary, POSITION_HELPER, POSITION_HELPER_END))
    cfg = analyze_cfg(executor_ops, EXECUTOR, EXECUTOR_END)
    helper_cfg = analyze_cfg(helper_ops, POSITION_HELPER, POSITION_HELPER_END)

    return {
        "schema": "fair-final-native-command-local-cfg-audit.v1",
        "status": "passed_limited_scope",
        "training_admissible": False,
        "library": {"path": str(path), "sha256": actual_sha, "size_bytes": path.stat().st_size,
                    "architecture": "AArch64"},
        "command_mapping": {"command_id": COMMAND_ID, "factory_start": hex(FACTORY_START),
                             "factory_table": hex(FACTORY_TABLE), "table_index": table_index,
                             "table_entry": hex(entry_address), "raw_table_word": hex(table_word),
                             "dispatch_base": hex(FACTORY_DISPATCH), "target": hex(factory_target),
                             "entry_shape": [{"address": hex(item.address), "instruction": item.mnemonic + " " + item.op_str}
                                             for item in command],
                             "constructor": hex(CONSTRUCTOR), "constructor_instruction_count": len(ctor),
                             "vtable": hex(VTABLE), "executor_vtable_slot": hex(VTABLE + 0x18),
                             "executor": hex(vtable_relocations[VTABLE + 0x18])},
        "executor_local_cfg": cfg,
        "position_helper_local_cfg": helper_cfg,
        "return_code_observations": sorted({
            item["value"] for ret in cfg["returns"] for item in ret["definitions"]
            if item["kind"] == "literal" and item["value"] is not None
        }),
        "dynamic_return_definitions": [
            {"ret": ret["address"], **definition}
            for ret in cfg["returns"] for definition in ret["definitions"]
            if definition["kind"] == "arithmetic-derived"
        ],
        "limitations": [
            "Local CFG only; branch predicates are not solved and dead paths are not excluded.",
            "Calls, including indirect virtual calls, are listed but not followed interprocedurally.",
            "Return values are instruction-level definitions, not fully decoded semantic error names.",
            "The live command-queue dispatch path and legal-mask equivalence are not established.",
            "This static audit does not prove native rejection reachability or general pending correctness.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.library)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
