import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

from tools.fair_final_prng_audit import (
    LIBG_SHA, NativePrngAudit, mapped_base, mapped_guest_pointer, parse_symbols)


PROBE_SHA = "49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba"
BASE_TIME = 0x19F3758
CACHED_TIME = 0x19F3760
REGIONS = {
    "initialize": (0x121B284, 0x121B2E4, "df9a34b3a4ebf9a2d27649d219f0b81acf5f3bf24784d8e29b1025aec46f00cf"),
    "seed": (0x121B2E4, 0x121B340, "6ee95dfb8fe45a385a1305291bf601f6ece3bb715d52e18fa9ecb61a99fa407f"),
    "constructor": (0x121B3DC, 0x121B40C, "da317bbc195e72fe695d31b2d56b553231c7d297b22bd517b71b700b6d073ae8"),
    "time_plt": (0x17E5260, 0x17E5270, "34d00afc4dedbfcb5a1002fdaff9292848b94961a4fcbb8389b6bca009ad7f8e"),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unsigned(value, bits):
    if type(value) is not int or not 0 <= value < 2**bits:
        raise ValueError("unsigned integer required")
    return value


def seed_effect(seed, base, cached, *, sampled_time=None):
    unsigned(seed, 32)
    unsigned(base, 64)
    unsigned(cached, 64)
    if cached == 0:
        if sampled_time is None:
            raise ValueError("zero cache requires the actual time call result")
        cached = unsigned(sampled_time, 64)
    elif sampled_time is not None:
        raise ValueError("nonzero cache does not call time")
    difference = (cached - base) % 2**64
    increment = int(difference > 15)
    return dict(cached_time=cached, unsigned_delta=difference, increment=increment,
        effective_seed=(seed + increment) % 2**32)


def static_report(path):
    import capstone
    import lief

    if sha(path) != LIBG_SHA:
        raise ValueError("seed audit library identity differs")
    binary = lief.parse(str(path))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    regions = {}
    for name, (start, end, expected) in REGIONS.items():
        content = bytes(binary.get_content_from_virtual_address(start, end - start))
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("seed audit instruction fingerprint differs")
        instructions = [dict(address=hex(item.address), mnemonic=item.mnemonic, op_str=item.op_str)
            for item in decoder.disasm(content, start)]
        if len(instructions) * 4 != end - start:
            raise ValueError("seed audit incomplete disassembly")
        regions[name] = dict(start=hex(start), end=hex(end), sha256=expected, instructions=instructions)
    tags = {}
    for entry in binary.dynamic_entries:
        if entry.tag in (lief.ELF.DynamicEntry.TAG.INIT_ARRAY, lief.ELF.DynamicEntry.TAG.INIT_ARRAYSZ):
            if str(entry.tag) in tags:
                raise ValueError("duplicate initializer dynamic tag")
            tags[str(entry.tag)] = entry.value
    if tags != {"TAG.INIT_ARRAY": 0x18E3DD8, "TAG.INIT_ARRAYSZ": 0x1788}:
        raise ValueError("initializer dynamic range differs")
    constructors = [entry for entry in binary.relocations if entry.address == 0x18E5278]
    imports = [entry for entry in binary.relocations if entry.address == 0x18E96F0]
    if (len(constructors) != 1 or constructors[0].type != lief.ELF.Relocation.TYPE.AARCH64_RELATIVE
            or constructors[0].addend != REGIONS["constructor"][0]
            or not 0x18E3DD8 <= constructors[0].address < 0x18E3DD8 + 0x1788
            or (constructors[0].address - 0x18E3DD8) % 8
            or len(imports) != 1 or imports[0].type != lief.ELF.Relocation.TYPE.AARCH64_JUMP_SLOT
            or not imports[0].has_symbol or imports[0].symbol.name != "time"):
        raise ValueError("seed constructor registration or time import differs")
    return dict(schema="fair-final-native-seed-static.v1", status="passed_limited_scope",
        libg_sha256=LIBG_SHA, regions=regions, initializer_tags=tags,
        constructor_slot=hex(constructors[0].address), time_import_slot=hex(imports[0].address),
        global_addresses=dict(base=hex(BASE_TIME), cached=hex(CACHED_TIME)),
        semantics="cache time only when zero; unsigned64(cache-base)>15 increments uint32 seed",
        all_global_writers_proven=False, native_lifecycle_verified=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)


class NativeSeedAudit(NativePrngAudit):
    def __init__(self, instance, *, probe, libg, port):
        self.instance = Path(instance).resolve()
        self.probe, self.libg = Path(probe).resolve(), Path(libg).resolve()
        self.record = json.loads((self.instance / "process.json").read_text())
        self.pid, self.port = int(self.record["child_pid"]), port
        for path, expected in ((self.probe, PROBE_SHA), (self.libg, LIBG_SHA)):
            if sha(path) != expected:
                raise ValueError("native seed audit binary identity differs")
        output = subprocess.run(["readelf", "-sW", str(self.probe)], check=True,
            capture_output=True, text=True, timeout=10)
        self.symbols = parse_symbols(output.stdout)
        self._verify_process()

    def inspect(self, identity=None):
        with self._paused() as process:
            maps = (process / "maps").read_text()
            probe_base, libg_base = mapped_base(maps, self.probe), mapped_base(maps, self.libg)
            descriptor = os.open(process / "mem", os.O_RDONLY)
            try:
                def read(address, format):
                    size = struct.calcsize(format)
                    content = os.pread(descriptor, size, address)
                    if len(content) != size:
                        raise ValueError("short native seed readonly memory read")
                    return struct.unpack(format, content)[0]
                def symbol(name, format):
                    return read(probe_base + self.symbols[name], format)
                actual = dict(generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), tick=symbol("tick", "<i"))
                if symbol("worker", "<B") or identity is not None and actual != {
                        name: identity[name] for name in actual}:
                    raise ValueError("stale or active native seed execution lane")
                bias = libg_base - symbol("libg", "<Q")
                def address(pointer, size):
                    return mapped_guest_pointer(pointer, bias, maps, size)
                base = read(libg_base + BASE_TIME, "<Q")
                cached = read(libg_base + CACHED_TIME, "<Q")
                state = None
                if identity is not None:
                    manager = address(symbol("manager", "<Q"), 0x220)
                    if read(manager + 0x60, "<i") != actual["tick"]:
                        raise ValueError("native seed manager tick differs")
                    state = read(manager + 0xa0, "<I")
                return dict(**actual, base_time=base, cached_time=cached, manager_rng=state,
                    increment=None if cached == 0 else seed_effect(0, base, cached)["increment"],
                    child_pid=self.pid, child_start_ticks=self.record["child_start_ticks"],
                    readonly=True, training_admissible=False, gates_closed=False)
            finally:
                os.close(descriptor)
