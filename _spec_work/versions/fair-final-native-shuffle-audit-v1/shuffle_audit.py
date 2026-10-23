import hashlib
import struct

from tools.fair_final_prng_audit import LIBG_SHA
from tools.fair_final_random_inventory import direct_branches


REGIONS = {
    "hand_wrapper": (0xf37828, 0xcc, "0a799a5e7f1bb4762bfe35af32d3128cc04390417caca0ff95f08f4447ffb045"),
    "hand_initialize": (0xf3ea70, 0x3f4, "46fc8e235bedbb14ab677011f8a31c94f9ef961287043e70aa1090801833778b"),
    "hand_shuffle": (0xf3f0ac, 0x774, "19624ca221e0cce53010de747edb5e46e90c2318741c50cffb5a377c2dc6cd83"),
    "identity_accessor": (0xcdf824, 8, "f1ed6ea80801eed17e4eb58f5f48a8a87c1517ac4ec3159dfe225107cd155900"),
    "identity_low": (0x121aa90, 8, "c40709113e97cec9c72bfbb7a6f1892a5b54d7906ca60d6c400c2e268577b883"),
    "world_random": (0xceeca0, 8, "0741c0c9762b0e17673225592aa32cbf4b1ac58b1c258941a34122441bc5b2e2"),
    "manager_random": (0x1062a1c, 8, "75b933f4596c3493d76bdc490a731a9efbf363b93930977af8fa7b78e905d5a3"),
    "mt_initialize": (0x121b118, 0x64, "9da4994dffa4ec8f5075ec4cbefa05c64f63a4b106dc0e1af4f0c2577d69951d"),
    "mt_clear": (0x121b17c, 0xc, "7869390d5e6edcef03e4a4f73a89596d9de46ad3d1cc3dd9de0253014a968cc8"),
    "debug_constructor": (0xd32fd4, 0x44, "db8b2c5b6190e00c9a4301a56ec2fcd02fef3681290a94e16fe9faab9334fae7"),
    "debug_executor": (0xd33018, 0x8460, "95bf9ca1a7e69990a1fb4e8db410d8581d247fb0718773ebd0577e52347a70e6"),
    "debug_type": (0xd3e8a0, 8, "a45f1ba91c8156add7836fbd94083adac0c4ab2dfe0289114535374c39c54a04"),
    "factory_prefix": (0xd2c138, 0x88, "2fd07b837a4a9de09f6f2a2b6f62e74d070fb73208d17e129f81b07d4785a6ba"),
    "debug_factory_arm": (0xd2d750, 0x28, "5b47498f39539d85953808a120b8e08d28bdbe5d4d7980b7d01781bf9f84c2c9"),
}
INITIALIZERS = {
    0x7f5434: (0x7f540c, 0x6c), 0xd35f9c: (0xd33018, 0x8460),
    0xdf47e8: (0xdf479c, 0xe0), 0xf3f498: (0xf3f0ac, 0x774),
    0xfaa2a4: (0xfaa238, 0x158), 0xfcdcf4: (0xfcdcb4, 0x90),
    0xfd0134: (0xfcf850, 0xa28), 0xfd01b8: (0xfcf850, 0xa28),
    0xfd16ac: (0xfd1440, 0x3e90), 0xfdb040: (0xfdaba4, 0xb48),
    0xfdeea8: (0xfdec94, 0x3cc), 0x1174854: (0x11747f0, 0x3f0),
    0x1175f44: (0x1175c68, 0xa88), 0x1176750: (0x11766f0, 0x208),
    0x1176958: (0x11768f8, 0x100), 0x1177e4c: (0x1177758, 0x1638),
    0x1179684: (0x117962c, 0x1b0), 0x117bb1c: (0x117b530, 0x64c),
}
ANCHORS = {
    0xf37848: [("bl", "#0xf2d294"), ("mov", "x3, x0"), ("add", "x0, x19, #0x210"),
        ("mov", "x1, x22"), ("mov", "w2, w21"), ("mov", "x4, x20"),
        ("mov", "x5, x19"), ("bl", "#0xf3ea70")],
    0xf3edc4: [("mov", "x4, sp"), ("mov", "x0, x19"), ("mov", "x1, x21"),
        ("mov", "x2, x22"), ("mov", "x3, x20"), ("bl", "#0xf3f0ac")],
    0xf3f470: [("ldr", "x0, [sp, #0x18]"), ("mov", "w1, #0xfffffff"),
        ("bl", "#0xceeca0"), ("mov", "w23, w0"), ("ldr", "x0, [sp, #8]"),
        ("bl", "#0xcdf824"), ("bl", "#0x121aa90"), ("mov", "w8, w0"),
        ("add", "x0, sp, #0x40"), ("add", "w1, w8, w23"), ("bl", "#0x121b118")],
    0xf3f4b8: [("add", "x0, sp, #0x40"), ("bl", "#0x121b188")],
    0xf3f6f4: [("add", "x0, sp, #0x40"), ("bl", "#0x121b188")],
    0xf3f7c4: [("add", "x0, sp, #0x40"), ("bl", "#0x121b17c")],
    0xd32ff4: [("adrp", "x8, #0x1877000"), ("add", "x8, x8, #0xbb0"), ("str", "x8, [x19]")],
    0xd330f4: [("ldr", "w8, [x19, #0x28]"), ("stur", "x20, [x29, #-0x70]"),
        ("cmp", "w8, #0x1ab"), ("b.hi", "#0xd3b428"), ("adrp", "x9, #0x342000"),
        ("add", "x9, x9, #0x74e"), ("adr", "x10, #0xd3311c"),
        ("ldrh", "w11, [x9, x8, lsl #1]"), ("add", "x10, x10, x11, lsl #2"), ("br", "x10")],
}


def normal_cleanup_paths(instructions, start, *, clear, generate):
    pending = [(start, False)]
    visited, returns, cleared_at = set(), set(), set()
    while pending:
        address, cleared = pending.pop()
        if (address, cleared) in visited:
            continue
        visited.add((address, cleared))
        if address not in instructions:
            raise ValueError("cleanup control flow leaves audited function")
        mnemonic, operands = instructions[address]
        successor = address + 4
        if mnemonic == "ret":
            if not cleared:
                raise ValueError("normal return bypasses random cleanup")
            returns.add(address)
            continue
        if mnemonic in ("br", "blr", "eret", "svc", "brk", "udf"):
            raise ValueError("unresolved control flow in random lifetime")
        if mnemonic == "bl":
            target = int(operands.removeprefix("#"), 0)
            if target == clear:
                if cleared:
                    raise ValueError("random object cleared twice")
                cleared = True
                cleared_at.add(address)
            elif target == generate and cleared:
                raise ValueError("random generation after cleanup")
        elif mnemonic == "b" or mnemonic.startswith("b.") or mnemonic in ("cbz", "cbnz", "tbz", "tbnz"):
            target = int(operands.rsplit("#", 1)[-1], 0)
            pending.append((target, cleared))
            if mnemonic == "b":
                continue
        pending.append((successor, cleared))
    if not returns or not cleared_at:
        raise ValueError("random lifetime has no verified normal cleanup return")
    return dict(reachable_instruction_states=len(visited),
        normal_returns=sorted(hex(address) for address in returns),
        clear_calls=sorted(hex(address) for address in cleared_at),
        assumption="direct calls return normally; exceptions, nonlocal exits and termination excluded")


def table_target(binary, *, table, index, count, base):
    if type(index) is not int or not 0 <= index < count:
        raise ValueError("dispatch index outside bounded table")
    content = bytes(binary.get_content_from_virtual_address(table + index * 2, 2))
    if len(content) != 2:
        raise ValueError("short dispatch table entry")
    entry = struct.unpack("<H", content)[0]
    return dict(table=hex(table), index=index, entry=entry, target=hex(base + entry * 4))


def build_report(path):
    import capstone
    import lief

    if hashlib.sha256(path.read_bytes()).hexdigest() != LIBG_SHA:
        raise ValueError("shuffle audit library identity differs")
    binary = lief.parse(str(path))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    regions, decoded = {}, {}
    for name, (start, size, expected) in REGIONS.items():
        content = bytes(binary.get_content_from_virtual_address(start, size))
        if len(content) != size or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("shuffle audit function fingerprint differs: " + name)
        instructions = {item.address: (item.mnemonic, item.op_str) for item in decoder.disasm(content, start)}
        if len(instructions) * 4 != size:
            raise ValueError("shuffle audit incomplete instruction coverage")
        decoded.update(instructions)
        regions[name] = dict(start=hex(start), size=size, sha256=expected)
    for start, expected in ANCHORS.items():
        if [decoded.get(start + offset * 4) for offset in range(len(expected))] != expected:
            raise ValueError("shuffle audit instruction anchor differs")
    start, size, expected = REGIONS["hand_shuffle"]
    lifecycle = normal_cleanup_paths({address: instruction for address, instruction in decoded.items()
        if start <= address < start + size}, 0xf3f49c, clear=0x121b17c, generate=0x121b188)
    if lifecycle["normal_returns"] != ["0xf3f7f4"] or lifecycle["clear_calls"] != ["0xf3f7c8"]:
        raise ValueError("shuffle audit normal cleanup exits differ")
    references = []
    for segment in binary.segments:
        if segment.type == lief.ELF.Segment.TYPE.LOAD and int(segment.flags) & 1:
            references.extend((location, opcode) for location, target, opcode in
                direct_branches(bytes(segment.content), segment.virtual_address) if target == 0x121b118)
    if sorted(location for location, opcode in references) != sorted(INITIALIZERS):
        raise ValueError("MT initializer reference census differs")
    functions = list(binary.functions)
    owners = []
    for location, opcode in sorted(references):
        start, size = INITIALIZERS[location]
        matching = [entry for entry in functions if entry.address <= location < entry.address + entry.size]
        if len(matching) != 1 or (matching[0].address, matching[0].size) != (start, size):
            raise ValueError("MT initializer function ownership differs")
        content = bytes(binary.get_content_from_virtual_address(start, size))
        if len(content) != size:
            raise ValueError("short MT initializer owner function")
        owners.append(dict(call=hex(location), opcode=opcode, owner=hex(start), size=size,
            sha256=hashlib.sha256(content).hexdigest(),
            role="hand_initialization" if location == 0xf3f498 else
                "debug_subtype55" if location == 0xd35f9c else "unresolved"))
    factory = table_target(binary, table=0x341b38, index=1548 - 2, count=1547, base=0xd2c174)
    subtype = table_target(binary, table=0x34274e, index=55, count=428, base=0xd3311c)
    if factory["target"] != "0xd2d750" or subtype["target"] != "0xd35f2c":
        raise ValueError("debug command dispatch target differs")
    relocations = []
    for slot, target in ((0x1877bc8, 0xd33018), (0x1877bd0, 0xd3e8a0)):
        matches = [entry for entry in binary.relocations if entry.address == slot]
        if (len(matches) != 1 or matches[0].type != lief.ELF.Relocation.TYPE.AARCH64_RELATIVE
                or matches[0].addend != target):
            raise ValueError("debug command vtable relocation differs")
        relocations.append(dict(slot=hex(slot), target=hex(target)))
    return dict(schema="fair-final-native-shuffle-audit.v1", status="passed_static_limited_scope",
        libg_sha256=LIBG_SHA, regions=regions, initializer_owners=owners,
        hand=dict(seed="uint32(world_manager_random(0x0fffffff) + identity_low_word)",
            identity_low_offset="0x24", temporary_rng="sp+0x40", normal_cleanup=lifecycle,
            time_dependency="MT initialization uses its seed argument; upstream world initialization audited separately",
            rule_caveat="hand_initialize has a rule-dependent alternate path; no universal shuffle claim"),
        debug=dict(command_type=1548, subtype=55, factory=factory, subtype_dispatch=subtype,
            vtable=relocations, reachable_in_fixed_deck_proven=False),
        scope="static direct initializer reference ownership and normal-return lifetime; not runtime coverage",
        unresolved=["16 remaining initializer references", "indirect calls and dispatch reachability",
            "all seed sources and other random algorithms", "runtime hand-path occurrence",
            "permutation law and prior calibration", "hidden-state noninterference for all reachable mechanisms"],
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)
