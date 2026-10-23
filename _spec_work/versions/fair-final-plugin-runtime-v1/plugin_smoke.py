import json
from pathlib import Path
import struct
import subprocess
import sys

from plugin_protocol import filter_spec, parse, sha


BASE = 0x40000000
ENTRY = 0x1000
FIRST = 0x121B118
SECOND = 0x121B2E4


def guest_bytes(*, interior=False):
    length = SECOND + 4
    content = bytearray(length)
    identity = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<16sHHIQQQIHHHHHH", content, 0, identity, 2, 183, 1,
        BASE + ENTRY, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", content, 64, 1, 5, 0, BASE, BASE, length, length, 4096)
    instructions = [0xD2800000 | (0x1234 << 5), 0xD2800001 | (77 << 5),
        0x94000000 | ((FIRST - (4 if interior else 0) - (ENTRY + 8)) // 4),
        0x94000000 | ((SECOND - (ENTRY + 12)) // 4),
        0xD2800000, 0xD2800008 | (93 << 5), 0xD4000001]
    for index, instruction in enumerate(instructions):
        struct.pack_into("<I", content, ENTRY + index * 4, instruction)
    for offset in (FIRST, SECOND):
        struct.pack_into("<I", content, offset, 0xD65F03C0)
    if interior:
        struct.pack_into("<I", content, FIRST - 4, 0xD2800000 | (0x5678 << 5))
    return bytes(content)


def main(root, *, interior=False, armed=False):
    settings = json.loads((root / "settings.json").read_text())
    guest = root / "register-smoke.elf"
    guest.write_bytes(guest_bytes(interior=interior))
    guest.chmod(0o700)
    output = root / "register-smoke.tsv"
    window = filter_spec(BASE)
    if armed:
        arm = root / "arm-base.txt"
        arm.write_text(f"{BASE:016x}\n")
        specification = f"{settings['plugin']},arm={arm},output={output}"
    else:
        specification = (f"{settings['plugin']},first={window['first_base']:x},"
            f"last={window['last_base']:x},output={output}")
    result = subprocess.run([settings["plugin_qemu"], "-plugin", specification, str(guest)],
        capture_output=True, timeout=30)
    (root / "register-smoke.stderr").write_bytes(result.stderr)
    if result.returncode:
        raise ValueError("synthetic AArch64 instruction smoke exited " + str(result.returncode))
    records = list(parse(output, base=BASE, center=BASE))
    if ([row["function"] for row in records] != ["mt_initialize", "xorshift_seed"]
            or any(row["object"] != (0x5678 if interior else 0x1234) or row["argument"] != 77 for row in records)
            or [row["return_address"] for row in records] != [BASE + ENTRY + 12, BASE + ENTRY + 16]):
        raise ValueError("synthetic plugin register values or callback sequence differ")
    proof = dict(schema="FairFinalPluginSyntheticSmoke/v1", guest_sha256=sha(guest),
        trace_sha256=sha(output), plugin_sha256=sha(settings["plugin"]),
        qemu_sha256=sha(settings["plugin_qemu"]), executed_callbacks=2,
        interior_instruction=interior, armed=armed,
        sampled_pc_differs=sum(row.get("sampled_pc", row["pc"]) != row["pc"] for row in records),
        synthetic_guest=True, native_game_evidence=False, training_admissible=False, gates_closed=False)
    (root / "register-smoke.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), interior="--interior" in sys.argv[2:], armed="--armed" in sys.argv[2:])
