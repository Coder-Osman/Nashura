import base64
import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from preference_sequence_capsule import build
from test_preference_cipher import cipher
from test_preference_sequence_capsule import reference


@pytest.fixture(scope="module")
def probe(tmp_path_factory, cipher):
    compiler = shutil.which("clang") or shutil.which("cc")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("sequence-probe") / "probe"
    subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", "-pthread",
        str(source / "preference_cipher.c"), str(source / "preference_sequence_probe.c"),
        "-o", str(output)], check=True)
    return output, cipher[1].decode()


def capsule():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv, plain = bytes(32), bytes(16), b"diagnostic-value"
    algorithm = Cipher(algorithms.AES(key), modes.CBC(iv))
    encoder = algorithm.encryptor()
    encrypted = encoder.update(plain + bytes([16]) * 16) + encoder.finalize()
    decoder = algorithm.decryptor()
    combined = decoder.update(bytes(16) + encrypted) + decoder.finalize()
    expected = combined[:-16]
    document = reference()
    for index, row in enumerate(document["direct_cipher"][4:]):
        if index != 1:
            row["input"] = base64.b64encode(encrypted).decode()
            row["result"]["output"] = base64.b64encode(expected if index == 2 else plain).decode()
    return build(document, key, iv)


def test_reports_backend_difference_and_preserves_recovery(probe, tmp_path):
    path = tmp_path / "sequence.bin"
    path.write_bytes(capsule())
    result = subprocess.run([str(probe[0]), probe[1], str(path)], capture_output=True)
    document = json.loads(result.stdout)
    assert document["records"] == 4
    assert [row["result"] for row in document["rows"]] == [0, -3, 0, 0]
    assert [row["length"] for row in document["rows"]] == [16, 0, 32, 16]
    assert all(row["output_guard"] for row in document["rows"])
    assert all(document["rows"][index]["matched"] for index in (0, 1, 3))
    matched = all(row["matched"] for row in document["rows"])
    assert document["status"] == ("PASS" if matched else "BLOCKED")
    assert result.returncode == (0 if matched else 2)
    assert b"diagnostic-value" not in result.stdout + result.stderr


@pytest.mark.parametrize("damage", ["magic", "truncated", "extra", "count", "failure", "length", "expected"])
def test_damage_or_mismatch_keeps_failure(probe, tmp_path, damage):
    data = bytearray(capsule())
    if damage == "magic": data[:4] = b"NCR1"
    elif damage == "truncated": data = data[:-1]
    elif damage == "extra": data += b"x"
    elif damage == "count": data[52:56] = struct.pack("<I", 65)
    elif damage == "failure": data[56:60] = struct.pack("<I", 2)
    elif damage == "length": data[60:64] = struct.pack("<I", 4113)
    elif damage == "expected": data[100] ^= 1
    path = tmp_path / "damaged.bin"
    path.write_bytes(data)
    result = subprocess.run([str(probe[0]), probe[1], str(path)], capture_output=True)
    assert result.returncode == 2
    document = json.loads(result.stdout)
    assert document["status"] == "BLOCKED"
    if damage == "expected":
        assert document["records"] == 4
        assert not document["rows"][0]["matched"]
        assert document["rows"][3]["matched"]
