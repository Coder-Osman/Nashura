import struct

import pytest

from preference_identity_material import extract_table, package_identity, private_write
from preference_method_audit import INITIALIZER


def fixture():
    dex = bytes(32) + struct.pack("<HHI24i", 0x300, 4, 24, *range(24))
    text = ("001000: |[001000] " + INITIALIZER + "\n"
            "001010: |0000: iput-object v0, v1, Lcom/supercell/titan/GameApp;.o:Lc/a;\n"
            "001012: |0002: iput-object v0, v1, Lcom/supercell/titan/GameApp;.c:Lc/a;\n"
            "001014: |0004: fill-array-data v3, 0010 // +000c\n"
            "001016: |0006: iput-object v0, v1, Lcom/supercell/titan/GameApp;.p:Lc/a;\n"
            "000020: |0010: array-data (52 units)\n      catches       : (none)\n")
    return text, dex


def test_private_array_extraction_and_slot_binding():
    table, checksum = extract_table(*fixture())
    assert table == list(range(24))
    assert len(checksum) == 64


@pytest.mark.parametrize("damage", ["slot", "missing", "duplicate", "layout", "truncated"])
def test_unknown_or_ambiguous_identity_material_rejected(damage):
    text, dex = fixture()
    if damage == "slot": text = text.replace(";.c:Lc/a;", ";.p:Lc/a;")
    if damage == "missing": text = text.replace("0010 //", "0011 //")
    if damage == "duplicate": text = text.replace("0010 // +000c", "0010 // +000c\n001018: |0008: fill-array-data v4, 0010 // +0008")
    if damage == "layout": dex = dex[:34] + b"\x02\x00" + dex[36:]
    if damage == "truncated": dex = dex[:-1]
    with pytest.raises(ValueError):
        extract_table(text, dex)


def test_transform_uses_utf16_indices_reversed_and_signed_table():
    assert package_identity("AB", [0] * 24) == b"21"
    assert package_identity("\U0001f600A", [0] * 24) == bytes([49, 48, 29 + 48])
    assert package_identity("A", [-1] * 24) == bytes([78])
    assert package_identity("A" * 25, list(range(24)))[:2] == bytes([49, 70])


@pytest.mark.parametrize("package,table", [("", [0] * 24), (None, [0] * 24),
    ("A", [0] * 23), ("A", [True] * 24), ("A", [2 ** 31] * 24), ("A" * 4097, [0] * 24)])
def test_unbound_or_unbounded_arguments_rejected(package, table):
    with pytest.raises(ValueError):
        package_identity(package, table)


def test_private_output_is_exclusive_and_not_world_readable(tmp_path):
    output = tmp_path / "material"
    private_write(output, b"synthetic")
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_write(output, b"replacement")
    assert output.read_bytes() == b"synthetic"
