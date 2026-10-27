import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from test_preference_cipher import cipher


@pytest.fixture(scope="module")
def probe(tmp_path_factory, cipher):
    compiler = shutil.which("clang") or shutil.which("cc")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("record-probe") / "probe"
    subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", "-pthread",
        str(source / "preference_cipher.c"), str(source / "preference_cipher_record_probe.c"),
        "-o", str(output)], check=True)
    return output, cipher[1].decode()


def capsule():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv = bytes(32), bytes(16)
    plain = b"synthetic-reference"
    padded = plain + bytes([16 - len(plain) % 16]) * (16 - len(plain) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    data = b"NCR1" + key + iv + struct.pack("<I", 2)
    for operation, input_bytes, expected in [(1, plain, encrypted), (2, encrypted, plain)]:
        data += struct.pack("<III", operation, len(input_bytes), len(expected)) + input_bytes + expected
    return data


def test_reference_probe_compares_bytes_without_printing_material(probe, tmp_path):
    path = tmp_path / "reference.bin"
    path.write_bytes(capsule())
    result = subprocess.run([str(probe[0]), probe[1], str(path)], capture_output=True, check=True)
    assert json.loads(result.stdout) == {"status": "PASS", "records": 2, "libg_loaded": False,
        "scope": "explicit_private_reference_bytes_only"}
    assert b"synthetic-reference" not in result.stdout + result.stderr


@pytest.mark.parametrize("damage", ["magic", "truncated", "extra", "count", "operation", "length", "expected"])
def test_damaged_or_mismatching_capsule_is_rejected(probe, tmp_path, damage):
    data = bytearray(capsule())
    if damage == "magic": data[0] ^= 1
    elif damage == "truncated": data = data[:-1]
    elif damage == "extra": data += b"x"
    elif damage == "count": data[52:56] = struct.pack("<I", 65)
    elif damage == "operation": data[56:60] = struct.pack("<I", 3)
    elif damage == "length": data[60:64] = struct.pack("<I", 4113)
    elif damage == "expected": data[-1] ^= 1
    path = tmp_path / "damaged.bin"
    path.write_bytes(data)
    result = subprocess.run([str(probe[0]), probe[1], str(path)], capture_output=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "BLOCKED"
