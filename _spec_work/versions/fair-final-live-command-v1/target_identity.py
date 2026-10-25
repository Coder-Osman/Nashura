import hashlib
from pathlib import Path


LIBRARY_SHA = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
OPCODES = {
    0xCFCF50: "ff8301d1", 0xCFD088: "c0035fd6", 0xD2E63C: "000b8052",
    0xD309A8: "fd7bbea9", 0xD30A00: "ff4302d1", 0xD30AC4: "c0035fd6",
    0xD30CCC: "a0000034",
}


def validate_targets(library):
    library = Path(library)
    if hashlib.sha256(library.read_bytes()).hexdigest() != LIBRARY_SHA:
        raise ValueError("live target library SHA differs")
    import lief

    binary = lief.parse(str(library))
    for offset, expected in OPCODES.items():
        actual = bytes(binary.get_content_from_virtual_address(offset, 4))
        if actual.hex() != expected:
            raise ValueError("live target instruction bytes differ")
    return dict(library_sha256=LIBRARY_SHA,
        instruction_bytes={hex(offset): value for offset, value in OPCODES.items()})
