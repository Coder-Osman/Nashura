import hashlib
import struct

from tools.fair_final_prng_audit import LIBG_SHA


TARGETS = {
    0x121b118: "mt_like_initialize",
    0x121b17c: "mt_like_clear",
    0x121b188: "mt_like_generate",
    0x121b284: "xorshift_initialize",
    0x121b2e4: "xorshift_seed",
    0x121b350: "xorshift_generate",
    0x121b394: "xorshift_save",
    0x121b3ac: "xorshift_restore",
}
ANCHORS = {
    "manager_random": (0x1062a1c, [("add", "x0, x0, #0xa0"), ("b", "#0x121b350")]),
    "world_manager_random": (0xceeca0, [("ldr", "x0, [x0, #8]"), ("b", "#0x1062a1c")]),
    "world_save_branch": (0x1062dc8, [("ldr", "x0, [x28, #0x48]"), ("cbz", "x0, #0x1062de4")]),
    "alternate_save": (0x1062e18, [("ldr", "x0, [x22, #0x50]"), ("mov", "x1, x20"), ("bl", "#0xf960d8")]),
    "world_restore_branch": (0x1063bc0, [("ldr", "x8, [x19, #0xa8]"), ("str", "w0, [x19, #0x9c]"), ("cbz", "x8, #0x1063bfc")]),
    "alternate_restore": (0x1063c44, [("ldr", "x0, [x19, #0x50]"), ("cbnz", "x0, #0x1063c6c")]),
    "alternate_rng_save": (0xf96ed0, [("add", "x0, x20, #0x550"), ("mov", "x1, x19"), ("bl", "#0x121b394")]),
    "alternate_rng_restore": (0xf98530, [("add", "x0, x19, #0x550"), ("mov", "x1, x20"), ("bl", "#0x121b3ac")]),
    "alternate_rng_save_554": (0xf96edc, [("add", "x0, x20, #0x554"), ("mov", "x1, x19"), ("bl", "#0x121b394")]),
    "alternate_rng_restore_554": (0xf9853c, [("add", "x0, x19, #0x554"), ("mov", "x1, x20"), ("bl", "#0x121b3ac")]),
    "alternate_rng_save_558": (0xf96ee8, [("add", "x0, x20, #0x558"), ("mov", "x1, x19"), ("bl", "#0x121b394")]),
    "alternate_rng_restore_558": (0xf98548, [("add", "x0, x19, #0x558"), ("mov", "x1, x20"), ("bl", "#0x121b3ac")]),
    "tile_object_save": (0xcf4e8c, [("ldr", "x0, [x19, #0x10]"), ("mov", "x1, x20"), ("bl", "#0x115cdc0")]),
    "tile_object_restore": (0xcf5fa4, [("ldr", "x0, [x19, #0x10]"), ("cbz", "x0, #0xcf6238"), ("mov", "x1, x20"), ("bl", "#0x115cdfc")]),
    "tick_seeded_mt_initialize": (0xd35f8c, [("mov", "x0, x20"), ("bl", "#0x1061a08"), ("ldr", "w1, [x0]"), ("mov", "x0, x25"), ("bl", "#0x121b118")]),
    "manager_tick_reference": (0x1061a08, [("add", "x0, x0, #0x60"), ("ret", "")]),
    "tick_seeded_mt_release": (0xd35ffc, [("mov", "x0, x25"), ("bl", "#0x121b17c"), ("mov", "x0, x25"), ("bl", "#0x17e4b50")]),
    "unit_component_random": (0xf4f794, [("add", "x0, x19, #0xc0"), ("mov", "w1, w21"), ("sub", "x23, x21, #1"), ("bl", "#0x121b350")]),
    "component_vtable": (0xf4efe4, [("adrp", "x8, #0x189f000"), ("add", "x8, x8, #0x900"),
        ("ldr", "q1, [x9, #0xc20]"), ("strb", "wzr, [x19, #0x70]"), ("str", "x8, [x19]")]),
    "component_seed_from_tick": (0xf4f100, [("ldr", "x0, [x19, #0x20]"), ("bl", "#0xf2d294"),
        ("ldr", "x0, [x0, #8]"), ("bl", "#0x1062bc0"), ("mov", "w8, #0x3039"),
        ("mul", "w1, w0, w8"), ("add", "x0, x19, #0xc0"), ("ldr", "x19, [sp, #0x10]"),
        ("ldp", "x29, x30, [sp], #0x20"), ("b", "#0x121b2e4")]),
    "manager_tick_value": (0x1062bc0, [("ldr", "w0, [x0, #0x60]"), ("ret", "")]),
}


def direct_branches(content, address):
    if address % 4 or len(content) % 4:
        raise ValueError("unaligned AArch64 executable range")
    for offset, (word,) in enumerate(struct.iter_unpack("<I", content)):
        opcode = word >> 26
        if opcode not in (5, 37):
            continue
        displacement = word & 0x3ffffff
        if displacement & 0x2000000:
            displacement -= 0x4000000
        location = address + offset * 4
        yield location, location + displacement * 4, "bl" if opcode == 37 else "b"


def build_inventory(path):
    import capstone
    import lief

    if hashlib.sha256(path.read_bytes()).hexdigest() != LIBG_SHA:
        raise ValueError("random inventory library identity differs")
    binary = lief.parse(str(path))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    anchors = {}
    for name, (address, expected) in ANCHORS.items():
        content = bytes(binary.get_content_from_virtual_address(address, len(expected) * 4))
        actual = [(entry.mnemonic, entry.op_str) for entry in decoder.disasm(content, address)]
        if actual != expected:
            raise ValueError(f"random inventory anchor changed: {name}")
        anchors[name] = dict(address=hex(address), bytes=content.hex(), instructions=actual)
    calls = {name: [] for name in TARGETS.values()}
    segments = []
    for segment in binary.segments:
        if segment.type != lief.ELF.Segment.TYPE.LOAD or not int(segment.flags) & 1:
            continue
        content = bytes(segment.content)
        segments.append(dict(address=hex(segment.virtual_address), bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest()))
        for location, target, opcode in direct_branches(content, segment.virtual_address):
            if target not in TARGETS:
                continue
            offset = location - segment.virtual_address
            start = max(0, offset - 32)
            context = [(hex(entry.address), entry.mnemonic, entry.op_str) for entry in
                decoder.disasm(content[start:offset + 4], segment.virtual_address + start)]
            calls[TARGETS[target]].append(dict(address=hex(location), opcode=opcode, context=context))
    if not segments or any(not entries for entries in calls.values()):
        raise ValueError("random inventory executable coverage incomplete")
    return dict(schema="fair-final-random-inventory.v1", status="passed",
        training_admissible=False, libg_sha256=LIBG_SHA, anchors=anchors, segments=segments, calls=calls,
        scope="direct B/BL references in executable PT_LOAD segments; not a reachability proof",
        unresolved=["indirect dispatch", "unit/component persistent state ownership",
            "transient generators and their runtime seed provenance", "other random algorithms"])
