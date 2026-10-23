import hashlib
import json
import os
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-native-seed-audit-v1"))
sys.path.insert(0, str(ROOT.parent / "fair-final-native-rng-alias-audit-v1"))
from seed_audit import LIBG_SHA, NativeSeedAudit, mapped_base, mapped_guest_pointer, sha


GLOBAL_OFFSET = 0x19FC368
STATE_SIZE = 0x9C8
PORT = 29779
REGIONS = {
    "constructor": (0x1335848, 8, "551a04adfa221ffdd710e3bab1d63703b76072feb987852d9a3a5c771b7ab906"),
    "fallthrough": (0x1A1CB0C, 40, "6a66e74fd9eccf18267bfbe09b288eef145184d95ba1215dd8d4167c89ab39cc"),
    "consumer_one_address": (0x133527C, 12, "938d351c62bca3349c5feccd01fe5f783c914a828b131f67618f5a626b75042f"),
    "consumer_one_call": (0x133530C, 12, "78c6ee069b31c2a4b464a73846fafdef4e8223e933df1fa81aadcf36a171b766"),
    "consumer_two": (0x133572C, 28, "cd12412ab7379e93cb29c5ad65bd57ba3b1b5f28e5cc0221701fd758b4a3886b"),
    "consumer_three_address": (0x1A1A7DC, 20, "f5ca9fc121b679b1da30228a0d6ef1b92cd32c8d04a438081f2ece612850009c"),
    "consumer_three_arg": (0x13351EC, 12, "801134c33258c44276a7148660631a52318292e1224423e520e1fe47667642c2"),
    "consumer_three_call": (0x7F88B4, 4, "7d48c9174a600c99237e45b5c18bde0cef756548fbd8dc1c8e2d0d2eba14331a"),
    "initialize": (0x121B118, 100, "9da4994dffa4ec8f5075ec4cbefa05c64f63a4b106dc0e1af4f0c2577d69951d"),
    "clear": (0x121B17C, 12, "7869390d5e6edcef03e4a4f73a89596d9de46ad3d1cc3dd9de0253014a968cc8"),
}


def perturb_state(content):
    if type(content) is not bytes or len(content) != STATE_SIZE:
        raise ValueError("exact native global state bytes required")
    result = bytearray(content)
    result[0] ^= 1
    return bytes(result)


def classify_restore(original, perturbed, restored):
    if perturb_state(original) != perturbed:
        raise ValueError("global perturbation contract differs")
    if restored == original:
        return "restored_original"
    if restored == perturbed:
        return "retained_perturbation"
    raise ValueError("global restore produced unexpected bytes")


def state_summary(content):
    if type(content) is not bytes or len(content) != STATE_SIZE:
        raise ValueError("exact native global state bytes required")
    return dict(sha256=hashlib.sha256(content).hexdigest(), size=len(content),
        first_word=struct.unpack_from("<I", content)[0],
        index=struct.unpack_from("<I", content, 0x9C0)[0],
        auxiliary=struct.unpack_from("<I", content, 0x9C4)[0])


def static_report(path):
    import capstone
    import lief
    from alias_audit import build_report

    parent = build_report(path)
    binary = lief.parse(str(path))
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    regions = {}
    for name, (start, size, expected) in REGIONS.items():
        content = bytes(binary.get_content_from_virtual_address(start, size))
        if len(content) != size or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("global RNG region differs: " + name)
        instructions = [(item.mnemonic, item.op_str) for item in decoder.disasm(content, start)]
        if len(instructions) * 4 != size:
            raise ValueError("global RNG incomplete instruction coverage")
        regions[name] = dict(start=hex(start), size=size, sha256=expected, instructions=instructions)
    if regions["constructor"]["instructions"] != [
            ("stp", "x29, x30, [sp, #-0x10]!"), ("bl", "#0x1a1cb0c")]:
        raise ValueError("global RNG constructor transfer differs")
    if regions["fallthrough"]["instructions"] != [
            ("ldp", "x29, x30, [sp], #0x10"), *parent["regions"]["time_seed_path"]["instructions"]]:
        raise ValueError("global RNG stack and fallthrough contract differs")
    slots = [entry for entry in binary.relocations if entry.address == 0x18E5370]
    tags = {str(entry.tag): entry.value for entry in binary.dynamic_entries
        if entry.tag in (lief.ELF.DynamicEntry.TAG.INIT_ARRAY, lief.ELF.DynamicEntry.TAG.INIT_ARRAYSZ)}
    if (tags != {"TAG.INIT_ARRAY": 0x18E3DD8, "TAG.INIT_ARRAYSZ": 0x1788}
            or len(slots) != 1 or slots[0].type != lief.ELF.Relocation.TYPE.AARCH64_RELATIVE
            or slots[0].addend != 0x1335848 or not 0x18E3DD8 <= slots[0].address < 0x18E5560
            or (slots[0].address - 0x18E3DD8) % 8):
        raise ValueError("global RNG constructor registration differs")
    return dict(schema="fair-final-native-global-rng-static.v1", status="passed_static_limited_scope",
        libg_sha256=LIBG_SHA, regions=regions, constructor_slot="0x18e5370", initializer_tags=tags,
        global_offset=hex(GLOBAL_OFFSET), state_size=STATE_SIZE,
        constructor_registration_proven=True,
        semantics="registered load initializer restores caller LR then falls through to time-seeded MT initialization",
        consumer_scope="three bound code paths pass the global address to MT generation; runtime usage not established",
        fixed_hog_usage_proven=False, snapshot_coverage_proven=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)


class NativeGlobalRngAudit(NativeSeedAudit):
    def global_state(self, identity=None, *, expected=None, replacement=None):
        if (expected is None) != (replacement is None):
            raise ValueError("global mutation requires expected and replacement bytes")
        if replacement is not None:
            state_summary(expected)
            state_summary(replacement)
        with self._paused() as process:
            maps = (process / "maps").read_text()
            probe_base, libg_base = mapped_base(maps, self.probe), mapped_base(maps, self.libg)
            descriptor = os.open(process / "mem", os.O_RDONLY if replacement is None else os.O_RDWR)
            try:
                def read(address, size):
                    content = os.pread(descriptor, size, address)
                    if len(content) != size:
                        raise ValueError("short global RNG memory read")
                    return content
                def symbol(name, format):
                    return struct.unpack(format, read(probe_base + self.symbols[name], struct.calcsize(format)))[0]
                actual = dict(generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), tick=symbol("tick", "<i"))
                if symbol("worker", "<B") or identity is not None and actual != {
                        name: identity[name] for name in actual}:
                    raise ValueError("stale or active global RNG execution lane")
                guest_base = symbol("libg", "<Q")
                address = mapped_guest_pointer(guest_base + GLOBAL_OFFSET,
                    libg_base - guest_base, maps, STATE_SIZE)
                if address != libg_base + GLOBAL_OFFSET:
                    raise ValueError("global RNG mapped address differs")
                if identity is not None:
                    manager = mapped_guest_pointer(symbol("manager", "<Q"),
                        libg_base - guest_base, maps, 0x220)
                    if struct.unpack("<i", read(manager + 0x60, 4))[0] != actual["tick"]:
                        raise ValueError("global RNG manager tick differs")
                content = read(address, STATE_SIZE)
                if replacement is not None:
                    if content != expected:
                        raise ValueError("global RNG compare-and-swap mismatch")
                    if os.pwrite(descriptor, replacement, address) != STATE_SIZE:
                        raise ValueError("short global RNG memory write")
                    if read(address, STATE_SIZE) != replacement:
                        raise ValueError("global RNG write readback differs")
                    content = replacement
                return content, dict(**actual, child_pid=self.pid,
                    child_start_ticks=self.record["child_start_ticks"], libg_sha256=LIBG_SHA,
                    global_offset=hex(GLOBAL_OFFSET), state=state_summary(content),
                    readonly=replacement is None)
            finally:
                os.close(descriptor)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = static_report(arguments.library)
    with arguments.output.open("x") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(dict(status=report["status"], sha256=sha(arguments.output))))
