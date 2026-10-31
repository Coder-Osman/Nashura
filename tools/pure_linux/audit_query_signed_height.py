"""Pinned original bytes and aligned direct-entry audit for one query block."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

from audit_component_guard import branch_target
from elf_audit import Elf64
from run_headless_match import ENGINE_SHA256


def audit(path):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != ENGINE_SHA256:
        raise ValueError("exact pinned engine required")
    elf = Elf64(data)
    header = Path(__file__).with_name("headless_query_dimension_pair.h").read_text()
    block = header.split("query_dimension_original[] = {", 1)[1].split("};", 1)[0]
    words = [int(x, 16) for x in re.findall(r"0x[0-9a-f]+", block)]
    body = elf.slice(elf.address(0xf34c6c, 76), 76)
    if len(words) != 19 or body != struct.pack("<19I", *words):
        raise ValueError("whole original query block differs")
    reject = elf.slice(elf.address(0xf34c60, 12), 12)
    if reject != struct.pack("<3I", 0x6b1c037f, 0x1100077b, 0x54fffe80):
        raise ValueError("reject landing overwrites NZCV before use")
    entries = []
    for segment in elf.segments:
        if segment["kind"] != 1 or not segment["flags"] & 1:
            continue
        for offset in range(segment["offset"], segment["offset"] + segment["filesz"] - 3, 4):
            pc = segment["virtual"] + offset - segment["offset"]
            target = branch_target(struct.unpack_from("<I", data, offset)[0], pc)
            if target is not None and 0xf34c6c <= target < 0xf34cb8 and not 0xf34c6c <= pc < 0xf34cb8:
                entries.append(dict(pc=hex(pc), target=hex(target)))
    if entries != [dict(pc="0xf34c58", target="0xf34c6c")]:
        raise ValueError("unexpected external direct entry into shifted query block")
    return dict(schema="nashura.query-signed-height-static-audit.v1", engine_sha256=ENGINE_SHA256,
        original_block_sha256=hashlib.sha256(body).hexdigest(), external_direct_entries=entries,
        reject_landing_sha256=hashlib.sha256(reject).hexdigest(),
        scope="aligned immediate branches in executable PT_LOAD; not arbitrary indirect-target proof")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.engine)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
