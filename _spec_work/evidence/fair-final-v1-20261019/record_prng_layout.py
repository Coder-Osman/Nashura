import hashlib
import json
from pathlib import Path

import capstone
import lief


def main():
    root = Path(__file__).resolve().parent
    source = root.parents[2] / "firstlight_CR/artifacts/standalone-engine-20260926/app-libs/libg.so"
    expected = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError("PRNG layout libg identity differs")
    binary = lief.parse(source)
    decoder = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    ranges = {"manager_random": (0x1062a1c, 8), "manager_tick": (0x1062bc0, 8),
        "snapshot_restore_call": (0x1063ba4, 12), "generator_save_restore": (0x121b350, 140)}
    evidence = {}
    for name, (address, size) in ranges.items():
        content = bytes(binary.get_content_from_virtual_address(address, size))
        instructions = [dict(address=hex(entry.address), mnemonic=entry.mnemonic, operands=entry.op_str)
            for entry in decoder.disasm(content, address)]
        if len(instructions) * 4 != size:
            raise ValueError("PRNG layout instruction coverage incomplete")
        evidence[name] = dict(address=hex(address), bytes=content.hex(), instructions=instructions)
    required = {"manager_random": [("add", "x0, x0, #0xa0"), ("b", "#0x121b350")],
        "manager_tick": [("ldr", "w0, [x0, #0x60]"), ("ret", "")],
        "snapshot_restore_call": [("add", "x0, x19, #0xa0"), ("mov", "x1, x20"), ("bl", "#0x121b3ac")]}
    for name, instructions in required.items():
        if [(entry["mnemonic"], entry["operands"]) for entry in evidence[name]["instructions"]] != instructions:
            raise ValueError("PRNG layout instruction contract differs")
    report = dict(schema="fair-final-prng-static-layout.v1", status="passed", libg_sha256=expected,
        source=str(source), audit_scope="GameStateManager inline PRNG only", ranges=evidence,
        generator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (root / "native-prng-layout.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(status="passed", libg_sha256=expected, instruction_ranges=len(ranges))))


if __name__ == "__main__":
    main()
