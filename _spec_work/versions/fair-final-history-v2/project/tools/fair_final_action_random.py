from collections import Counter
import hashlib
import os
import struct

from tools.fair_final_prng_audit import LIBG_SHA, NativePrngAudit, mapped_base, mapped_guest_pointer


TARGET_VTABLE = 0x189f900
CLASS_NAME = "logic.data.actions.LogicActionGoblinsteinAbilityData"


def static_layout(path):
    import capstone
    import lief

    if hashlib.sha256(path.read_bytes()).hexdigest() != LIBG_SHA:
        raise ValueError("action random binary identity differs")
    binary = lief.parse(str(path))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    relocations = {entry.address: entry.addend for entry in binary.relocations}
    expected_slots = {0x188f2c8: 0xe6596c, 0x188f328: 0xe64fb8,
        0x189f918: 0xf4f0ec, 0x189f9e8: 0xf4f9b4, 0x189f9f0: 0xf4faa0}
    if any(relocations.get(address) != target for address, target in expected_slots.items()):
        raise ValueError("action random virtual dispatch differs")
    name = bytes(binary.get_content_from_virtual_address(0x2d2dcb, 128)).split(b"\0")[0].decode()
    if name != CLASS_NAME:
        raise ValueError("action random reflected class differs")
    ranges = {"factory": (0xe64fb8, 0x44), "constructor": (0xf4efc8, 0x58),
        "base_constructor": (0xf0e2d8, 0x30), "reflection": (0xe6596c, 0xa4),
        "seed_argument": (0xf4f0ec, 0x3c), "seed_setter": (0x121b2e4, 0x5c),
        "save": (0xf4f9b4, 0xec), "restore": (0xf4faa0, 0x180),
        "base_save": (0xf0e634, 0x7c), "base_restore": (0xf0e6b0, 0x88),
        "generator_call": (0xf4f794, 0x10)}
    evidence = {}
    for label, (address, size) in ranges.items():
        content = bytes(binary.get_content_from_virtual_address(address, size))
        entries = [dict(address=hex(entry.address), mnemonic=entry.mnemonic, operands=entry.op_str)
            for entry in decoder.disasm(content, address)]
        if len(entries) * 4 != size:
            raise ValueError("action random instruction coverage incomplete")
        evidence[label] = dict(bytes=content.hex(), instructions=entries)
    anchors = {0xe64fe4: ("bl", "#0xf4efc8"), 0xe659a8: ("adrp", "x1, #0x2d2000"),
        0xe659ac: ("add", "x1, x1, #0xdcb"), 0xf4eff4: ("str", "x8, [x19]"),
        0xf4f124: ("b", "#0x121b2e4"), 0xf0e2fc: ("stp", "x2, x1, [x0, #0x18]"),
        0xf4f7a0: ("bl", "#0x121b350"), 0xf4fc1c: ("ret", "")}
    instructions = {int(entry["address"], 16): (entry["mnemonic"], entry["operands"])
        for section in evidence.values() for entry in section["instructions"]}
    if any(instructions.get(address) != expected for address, expected in anchors.items()):
        raise ValueError("action random instruction anchor differs")
    return dict(schema="fair-final-action-random-layout.v1", libg_sha256=LIBG_SHA,
        runtime_vtable=hex(TARGET_VTABLE), reflected_class=name,
        dispatch={hex(address): hex(target) for address, target in expected_slots.items()}, ranges=evidence,
        limitation="direct serializers omit +0xc0; indirect or enclosing snapshot handling not proven",
        seed_caveat="tick*12345 is the argument; setter also reads global time-related state")


def walk_actions(read, resolve, manager, libg_guest):
    world = resolve(read(manager + 0xa8, "<Q"), 0xe8)
    if resolve(read(world + 8, "<Q"), 0x220) != manager:
        raise ValueError("action inventory world backlink differs")
    tower = resolve(read(world + 0xe0, "<Q"), 0x18)
    objects = resolve(read(tower + 0x10, "<Q"), 0x18)

    def vector(parent, offset, maximum):
        pointer = read(parent + offset, "<Q")
        capacity, count = read(parent + offset + 8, "<i"), read(parent + offset + 12, "<i")
        if not 0 <= count <= capacity <= maximum:
            raise ValueError("invalid action inventory vector bounds")
        if not count:
            return []
        data = resolve(pointer, count * 8)
        return [read(data + index * 8, "<Q") for index in range(count)]

    counts, matches, identities = Counter(), [], set()
    object_pointers = vector(objects, 8, 100000)
    queue_count, runtime_count = 0, 0
    for pointer in object_pointers:
        if not pointer:
            continue
        entity = resolve(pointer, 0xb0)
        identity = read(entity + 8, "<I")
        if not identity or identity in identities:
            raise ValueError("action inventory entity identity ambiguous")
        identities.add(identity)
        queue_pointer = read(entity + 0x28, "<Q")
        if not queue_pointer:
            continue
        queue = resolve(queue_pointer, 0x60)
        queue_count += 1
        for slot, runtime_pointer in enumerate(vector(queue, 0x50, 256)):
            if not runtime_pointer:
                raise ValueError("null active action runtime")
            runtime = resolve(runtime_pointer, 0x28)
            if resolve(read(runtime + 0x20, "<Q"), 0xb0) != entity:
                raise ValueError("action runtime owner backlink differs")
            vtable = (read(runtime, "<Q") & 0x00ffffffffffffff) - libg_guest
            if not 0 <= vtable < 0x2000000:
                raise ValueError("action runtime vtable outside fixed library")
            counts[hex(vtable)] += 1
            runtime_count += 1
            if vtable == TARGET_VTABLE:
                runtime = resolve(runtime_pointer, 0xc8)
                matches.append(dict(entity_id=identity, owner=read(entity + 0x78, "<i"),
                    card_id=read(entity + 0xac, "<i"), slot=slot, state=read(runtime + 0xc0, "<I")))
    return dict(object_count=len(identities), queue_count=queue_count, runtime_count=runtime_count,
        vtables=dict(sorted(counts.items())), matches=matches)


class NativeActionRandomAudit(NativePrngAudit):
    def inspect(self, identity):
        with self._paused() as process:
            maps = (process / "maps").read_text()
            probe_base, libg_base = mapped_base(maps, self.probe), mapped_base(maps, self.libg)
            descriptor = os.open(process / "mem", os.O_RDONLY)
            try:
                def read(address, format):
                    size = struct.calcsize(format)
                    content = os.pread(descriptor, size, address)
                    if len(content) != size:
                        raise ValueError("short action inventory memory read")
                    return struct.unpack(format, content)[0]

                def symbol(key, format):
                    return read(probe_base + self.symbols[key], format)

                actual = dict(generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), tick=symbol("tick", "<i"))
                if actual != {key: identity[key] for key in actual} or symbol("worker", "<B"):
                    raise ValueError("stale or active action inventory lane")
                libg_guest = symbol("libg", "<Q")
                bias = libg_base - libg_guest

                def resolve(pointer, size):
                    if not pointer:
                        raise ValueError("null action inventory pointer")
                    return mapped_guest_pointer(pointer, bias, maps, size)

                manager = resolve(symbol("manager", "<Q"), 0x220)
                if read(manager + 0x60, "<i") != actual["tick"]:
                    raise ValueError("action inventory manager tick differs")
                return dict(**actual, **walk_actions(read, resolve, manager, libg_guest), readonly=True,
                    child_pid=self.pid, child_start_ticks=self.record["child_start_ticks"])
            finally:
                os.close(descriptor)
