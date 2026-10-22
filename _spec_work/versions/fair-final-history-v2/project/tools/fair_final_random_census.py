import os
import struct

from tools.fair_final_prng_audit import NativePrngAudit, mapped_base, mapped_guest_pointer


def semantic_census(record):
    return {key: value for key, value in record.items() if key not in ("generation", "stateEpoch")}


class NativeRandomCensus(NativePrngAudit):
    def inspect(self, identity):
        with self._paused() as process:
            maps = (process / "maps").read_text()
            probe_base = mapped_base(maps, self.probe)
            libg_base = mapped_base(maps, self.libg)
            descriptor = os.open(process / "mem", os.O_RDONLY)
            try:
                def read(address, format):
                    size = struct.calcsize(format)
                    content = os.pread(descriptor, size, address)
                    if len(content) != size:
                        raise ValueError("short random census memory read")
                    return struct.unpack(format, content)[0]

                def symbol(key, format):
                    return read(probe_base + self.symbols[key], format)

                actual = dict(generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), tick=symbol("tick", "<i"))
                if actual != {key: identity[key] for key in actual} or symbol("worker", "<B"):
                    raise ValueError("stale or active random census execution lane")
                bias = libg_base - symbol("libg", "<Q")
                pointer = symbol("manager", "<Q")
                manager = mapped_guest_pointer(pointer, bias, maps, 0x220)
                if not pointer or read(manager + 0x60, "<i") != actual["tick"]:
                    raise ValueError("random census manager tick differs")
                world_pointer = read(manager + 0xa8, "<Q")
                if not world_pointer:
                    raise ValueError("random census requires an active battle world")
                world = mapped_guest_pointer(world_pointer, bias, maps, 0x20)
                parent = mapped_guest_pointer(read(world + 8, "<Q"), bias, maps, 0x220)
                if parent != manager:
                    raise ValueError("random census world belongs to another manager")
                alternate = read(manager + 0x50, "<Q")
                alternate_values = None
                if alternate:
                    address = mapped_guest_pointer(alternate, bias, maps, 0x570)
                    alternate_values = {hex(offset): read(address + offset, "<I")
                        for offset in (0x550, 0x554, 0x558)}
                return dict(**actual, manager_state=read(manager + 0xa0, "<I"),
                    world_present=True, world_backlink_matches=True,
                    alternate_50_present=bool(alternate), alternate_50_states=alternate_values,
                    alternate_218_present=bool(read(manager + 0x218, "<Q")),
                    tile_object_present=bool(read(world + 0x10, "<Q")),
                    readonly=True, child_pid=self.pid, child_start_ticks=self.record["child_start_ticks"])
            finally:
                os.close(descriptor)
