"""Reproduce the pinned ELF byte and direct-entry audit without executing it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
from elf_audit import Elf64
from run_headless_match import ENGINE_SHA256


def branch_target(word, pc):
    if word & 0x7c000000 == 0x14000000:
        bits, immediate = 26, word & 0x3ffffff
    elif word & 0xff000010 == 0x54000000 or word & 0x7e000000 == 0x34000000:
        bits, immediate = 19, (word >> 5) & 0x7ffff
    elif word & 0x7e000000 == 0x36000000:
        bits, immediate = 14, (word >> 5) & 0x3fff
    else:
        return None
    if immediate & (1 << (bits - 1)):
        immediate -= 1 << bits
    return pc + immediate * 4


def audit(path):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != ENGINE_SHA256:
        raise ValueError("exact pinned engine required")
    elf = Elf64(data)
    header = Path(__file__).with_name("headless_component_guard.h").read_text()
    block = header.split("component_guard_original[] = {", 1)[1].split("};", 1)[0]
    words = [int(x, 16) for x in re.findall(r"0x[0-9a-f]+", block)]
    body = data[elf.address(0x115d540, 196):elf.address(0x115d540, 196) + 196]
    if len(words) != 49 or body != struct.pack("<49I", *words):
        raise ValueError("whole original function guard differs from pinned ELF")
    pure = {0xf20210: (0x528000a0, 0xd65f03c0),
            0xf1b9e0: (0xb9411c00, 0xd65f03c0),
            0xf171b4: (0xf9402400, 0xd65f03c0),
            0xd98ca4: (0xf9413000, 0xd65f03c0)}
    for address, expected in pure.items():
        if elf.slice(elf.address(address, 8), 8) != struct.pack("<2I", *expected):
            raise ValueError("pure getter bytes differ")
    entries = []
    for segment in elf.segments:
        if segment["kind"] != 1 or not segment["flags"] & 1:
            continue
        for offset in range(segment["offset"], segment["offset"] + segment["filesz"] - 3, 4):
            pc = segment["virtual"] + offset - segment["offset"]
            word = struct.unpack_from("<I", data, offset)[0]
            if branch_target(word, pc) == 0x115d568:
                entries.append(hex(pc))
    if entries:
        raise ValueError("unexpected direct entry into replaced prologue word")
    return {"schema": "nashura.component-guard-static-audit.v1",
        "engine_sha256": ENGINE_SHA256, "component_bytes_sha256": hashlib.sha256(body).hexdigest(),
        "external_direct_entries_to_patched_word": entries,
        "pure_getters": [hex(x) for x in pure], "patched_offset": "0x115d568",
        "scope": "aligned immediate branches in executable PT_LOAD; not arbitrary indirect-target proof"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.engine)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write(chr(10))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
