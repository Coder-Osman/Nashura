import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent / "fair-final-history-v2/project"
sys.path.insert(0, str(PROJECT))

from tools.fair_final_prng_audit import (
    LIBG_SHA, NativePrngAudit, mapped_base, mapped_guest_pointer, parse_symbols)


PROBE_SHA = "49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba"
PREDICATE_START = 0x1062F6C
PREDICATE_END = 0x1062FD8
PREDICATE = [
    ("ldr", "x8, [x0, #0xa8]"), ("cbz", "x8, #0x1062fa0"),
    ("ldr", "w9, [x8, #0x18]"), ("cmp", "w9, #3"), ("b.eq", "#0x1062fc8"),
    ("cmp", "w9, #1"), ("b.ne", "#0x1062fd0"),
    ("ldr", "x8, [x8, #0x1b0]"), ("ldr", "w8, [x8, #8]"),
    ("cmp", "w8, #0xf"), ("ccmp", "w8, #6, #4, ne"), ("cset", "w0, eq"), ("ret", ""),
    ("ldr", "x0, [x0, #0x218]"), ("cbz", "x0, #0x1062fc8"),
    ("stp", "x29, x30, [sp, #-0x10]!"), ("mov", "x29, sp"), ("bl", "#0x107c8a4"),
    ("ldr", "w8, [x0, #0x68]"), ("cmp", "w8, #0"), ("cset", "w0, ne"),
    ("ldp", "x29, x30, [sp], #0x10"), ("ret", ""),
    ("mov", "w0, #1"), ("ret", ""), ("mov", "w0, wzr"), ("ret", ""),
]


def verify_predicate(instructions):
    actual = [(item.mnemonic, item.op_str) for item in instructions]
    if (actual != PREDICATE or len(instructions) * 4 != PREDICATE_END - PREDICATE_START
            or any(item.address != PREDICATE_START + index * 4 for index, item in enumerate(instructions))):
        raise ValueError("native queue mode predicate fingerprint differs")


def active_mode(world_type, rules_mode=None):
    if type(world_type) is not int or not 0 <= world_type < 2**32:
        raise ValueError("unsigned world type required")
    if world_type == 1 and (type(rules_mode) is not int or not 0 <= rules_mode < 2**32):
        raise ValueError("type1 mode requires unsigned native rules field")
    if world_type != 1 and rules_mode is not None:
        raise ValueError("unused rules field must stay absent")
    result = world_type == 3 or world_type == 1 and rules_mode in (6, 15)
    return dict(world_type=world_type, rules_mode=rules_mode, predicate_bit0=int(result),
        queue_branch="bit0-set" if result else "bit0-clear-age20",
        predicate_entry=hex(PREDICATE_START), native_world_required=True)


class NativeModeAudit(NativePrngAudit):
    def __init__(self, instance, *, probe, libg, port):
        self.instance = Path(instance).resolve()
        self.probe, self.libg = Path(probe).resolve(), Path(libg).resolve()
        self.record = json.loads((self.instance / "process.json").read_text())
        self.pid, self.port = int(self.record["child_pid"]), port
        for path, expected in ((self.probe, PROBE_SHA), (self.libg, LIBG_SHA)):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("native mode audit binary identity differs")
        symbols = subprocess.run(["readelf", "-sW", str(self.probe)], check=True,
            capture_output=True, text=True, timeout=10)
        self.symbols = parse_symbols(symbols.stdout)
        self._verify_process()

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
                        raise ValueError("short native mode readonly memory read")
                    return struct.unpack(format, content)[0]
                def symbol(name, format):
                    return read(probe_base + self.symbols[name], format)
                actual = dict(generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), tick=symbol("tick", "<i"))
                if actual != {name: identity[name] for name in actual} or symbol("worker", "<B"):
                    raise ValueError("stale or active native mode execution lane")
                bias = libg_base - symbol("libg", "<Q")
                def address(pointer, size):
                    return mapped_guest_pointer(pointer, bias, maps, size)
                manager = address(symbol("manager", "<Q"), 0x220)
                if read(manager + 0x60, "<i") != actual["tick"]:
                    raise ValueError("native mode manager tick differs")
                world = address(read(manager + 0xa8, "<Q"), 0x1b8)
                if address(read(world + 8, "<Q"), 0x220) != manager:
                    raise ValueError("native mode world manager backlink differs")
                world_type = read(world + 0x18, "<I")
                rules = read(address(read(world + 0x1b0, "<Q"), 12) + 8, "<I") if world_type == 1 else None
                return dict(**actual, **active_mode(world_type, rules), readonly=True,
                    child_pid=self.pid, child_start_ticks=self.record["child_start_ticks"],
                    world_backlink_matches=True, training_admissible=False, gates_closed=False)
            finally:
                os.close(descriptor)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if hashlib.sha256(args.library.read_bytes()).hexdigest() != LIBG_SHA:
        raise ValueError("native mode library identity differs")
    dependency = ROOT.parent / "fair-final-native-command-audit-v1/audit_ct86.py"
    if hashlib.sha256(dependency.read_bytes()).hexdigest() != "e70ce714bd8d6a266c8a0990ad5ebe63e8a40edb5e0a957e4d3c4292c6c84fe5":
        raise ValueError("native mode disassembler identity differs")
    spec = importlib.util.spec_from_file_location("mode_command_dependency", dependency)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import lief
    instructions = module._disassemble(lief.parse(str(args.library)), PREDICATE_START, PREDICATE_END)
    verify_predicate(instructions)
    report = dict(schema="fair-final-native-mode-static.v1", status="passed_limited_scope",
        libg_sha256=LIBG_SHA, instructions=[dict(address=hex(item.address),
            mnemonic=item.mnemonic, op_str=item.op_str) for item in instructions],
        predicate_scope="active-manager-world-only", native_observation_verified=False,
        legal_rejection_reachability_proven=False, training_admissible=False, gates_closed=False)
    if args.output.exists():
        raise ValueError("native mode evidence output already exists")
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({name: value for name, value in report.items() if name != "instructions"}))


if __name__ == "__main__":
    main()
