import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-native-shuffle-audit-v1"))
from shuffle_audit import INITIALIZERS, build_report as shuffle_report
from tools.fair_final_prng_audit import LIBG_SHA


INITIALIZE = 0x121B118
ALLOCATE = 0x17E4B20
SLICES = {
    0x7F5434: (0x7F5434, "incoming"),
    0xD35F9C: (0xD35F78, "allocation_return"),
    0xDF47E8: (0xDF47B8, "allocation_return"),
    0xF3F498: (0xF3F470, "stack_relative"),
    0xFAA2A4: (0xFAA28C, "stack_relative"),
    0xFCDCF4: (0xFCDCD0, "stack_relative"),
    0xFD0134: (0xFD0110, "allocation_return"),
    0xFD01B8: (0xFD0194, "allocation_return"),
    0xFD16AC: (0xFD1688, "allocation_return"),
    0xFDB040: (0xFDB038, "stack_relative"),
    0xFDEEA8: (0xFDEE98, "stack_relative"),
    0x1174854: (0x1174834, "allocation_return"),
    0x1175F44: (0x1175F0C, "stack_relative"),
    0x1176750: (0x117671C, "stack_relative"),
    0x1176958: (0x1176924, "stack_relative"),
    0x1177E4C: (0x1177E14, "stack_relative"),
    0x1179684: (0x1179650, "stack_relative"),
    0x117BB1C: (0x117BB04, "stack_relative"),
}


def canonical(register):
    if register in ("sp", "wsp"):
        return "sp"
    if register in ("xzr", "wzr"):
        return "zero"
    if re.fullmatch(r"[xw](?:[0-9]|[12][0-9]|30)", register):
        return "x" + register[1:]
    raise ValueError("unsupported general register: " + register)


def register_value(register, state):
    key = canonical(register)
    value = dict(kind="constant", value=0) if key == "zero" else state[key]
    return dict(kind="low32", value=value) if register.startswith("w") else value


def slice_arguments(instructions):
    if not instructions:
        raise ValueError("empty initializer argument slice")
    first = instructions[0].address
    if any(entry.address != first + index * 4 or entry.size != 4
            for index, entry in enumerate(instructions)):
        raise ValueError("noncontiguous initializer argument slice")
    final = instructions[-1]
    if final.mnemonic not in ("b", "bl") or final.op_str != hex_target(INITIALIZE):
        raise ValueError("argument slice does not end at MT initialization")
    state = {name: dict(kind="live_in", register=name, at=hex(first))
        for name in [*("x" + str(index) for index in range(31)), "sp"]}
    for entry in instructions[:-1]:
        operands = entry.op_str.split(", ")
        mnemonic = entry.mnemonic
        if mnemonic == "bl":
            if not re.fullmatch(r"#0x[0-9a-f]+", entry.op_str):
                raise ValueError("unresolved direct call in argument slice")
            for index in range(19):
                state["x" + str(index)] = dict(kind="call_clobbered",
                    register="x" + str(index), at=hex(entry.address))
            state["x0"] = dict(kind="call_return", target=entry.op_str,
                at=hex(entry.address), argument_semantics="not_inferred")
            state["x30"] = dict(kind="call_link", at=hex(entry.address))
            continue
        if (mnemonic in ("b", "br", "blr", "ret", "eret", "svc", "brk", "udf")
                or mnemonic.startswith("b.") or mnemonic in ("cbz", "cbnz", "tbz", "tbnz")):
            raise ValueError("control transfer inside initializer argument slice")
        _, writes = entry.regs_access()
        changed = [entry.reg_name(register) for register in writes]
        general = [register for register in changed
            if register in ("sp", "wsp") or re.fullmatch(r"[xw](?:[0-9]|[12][0-9]|30)", register)]
        destination = operands[0]
        value = None
        if mnemonic == "mov" and len(operands) == 2 and destination in general:
            source = operands[1]
            value = (dict(kind="constant", value=int(source[1:], 0)) if source.startswith("#")
                else register_value(source, state))
        elif mnemonic in ("add", "sub") and len(operands) == 3 and destination in general:
            source = operands[2]
            right = (dict(kind="constant", value=int(source[1:], 0)) if source.startswith("#")
                else register_value(source, state))
            value = dict(kind=mnemonic, left=register_value(operands[1], state), right=right)
        for register in general:
            expression = value if register == destination and value is not None else dict(
                kind="opaque_write", register=register, at=hex(entry.address),
                instruction=mnemonic + " " + entry.op_str)
            state[canonical(register)] = dict(kind="zero_extend32", value=expression) if (
                register.startswith("w")) else expression
    return dict(object=register_value("x0", state), seed=register_value("w1", state),
        assumption="fallthrough slice entry and normally returning AAPCS64 calls; no callee-body inference")


def hex_target(address):
    return "#" + hex(address)


def object_kind(expression):
    if expression.get("kind") == "live_in":
        return "stack_relative" if expression.get("register") == "sp" else "incoming"
    if expression.get("kind") == "call_return" and expression.get("target") == hex_target(ALLOCATE):
        return "allocation_return"
    if (expression.get("kind") in ("add", "sub")
            and expression["left"].get("kind") == "live_in"
            and expression["left"].get("register") == "sp"
            and expression["right"].get("kind") == "constant"):
        return "stack_relative"
    return "unresolved"


def build_report(library):
    import capstone
    import lief

    library = Path(library)
    if hashlib.sha256(library.read_bytes()).hexdigest() != LIBG_SHA:
        raise ValueError("callsite audit library identity differs")
    parent = shuffle_report(library)
    if set(SLICES) != set(INITIALIZERS):
        raise ValueError("initializer slice inventory differs from verified census")
    binary = lief.parse(str(library))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    decoder.detail = True
    allocation_plt = list(decoder.disasm(bytes(binary.get_content_from_virtual_address(ALLOCATE, 16)),
        ALLOCATE))
    if [(entry.mnemonic, entry.op_str) for entry in allocation_plt] != [
            ("adrp", "x16, #0x18e9000"), ("ldr", "x17, [x16, #0x350]"),
            ("add", "x16, x16, #0x350"), ("br", "x17")]:
        raise ValueError("allocation PLT instructions differ")
    allocation_imports = [entry for entry in binary.relocations if entry.address == 0x18E9350]
    if (len(allocation_imports) != 1 or not allocation_imports[0].has_symbol
            or allocation_imports[0].symbol.name != "_Znwm"
            or allocation_imports[0].type != lief.ELF.Relocation.TYPE.AARCH64_JUMP_SLOT):
        raise ValueError("allocation PLT does not resolve to operator new")
    records = []
    for location, (start, expected_kind) in sorted(SLICES.items()):
        size = location - start + 4
        content = bytes(binary.get_content_from_virtual_address(start, size))
        instructions = list(decoder.disasm(content, start))
        if len(content) != size or len(instructions) * 4 != size:
            raise ValueError("incomplete initializer argument bytes")
        arguments = slice_arguments(instructions)
        actual_kind = object_kind(arguments["object"])
        if actual_kind != expected_kind:
            raise ValueError("initializer object provenance differs: " + hex(location))
        records.append(dict(callsite=hex(location), slice_start=hex(start), size=size,
            sha256=hashlib.sha256(content).hexdigest(), object_kind=actual_kind,
            arguments=arguments, instructions=[dict(address=hex(entry.address),
                mnemonic=entry.mnemonic, operands=entry.op_str) for entry in instructions],
            fixed_hog_reachable_proven=False, escapes_or_callee_retention_proven=False,
            snapshot_coverage_proven=False))
    return dict(schema="FairFinalInitializerArguments/v1", status="verified_static_limited_scope",
        libg_sha256=LIBG_SHA, parent_schema=parent["schema"], direct_initializer_count=len(records),
        allocation_import=dict(plt=hex(ALLOCATE), slot="0x18e9350", name="_Znwm"),
        counts={kind: sum(record["object_kind"] == kind for record in records)
            for kind in ("incoming", "stack_relative", "allocation_return")}, records=records,
        scope="local fallthrough arguments for all 18 known direct MT initializer references",
        unresolved=["slice entry reachability and indirect transfers", "live-in and opaque seed origins",
            "callee pointer retention and object lifetime", "fixed-deck runtime usage",
            "all other random algorithms and snapshot coverage"],
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = build_report(arguments.library)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({name: report[name] for name in ("status", "direct_initializer_count", "counts")}))


if __name__ == "__main__":
    main()
