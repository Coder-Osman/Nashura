import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from preference_sequence_capsule import CASES, ROUTE, build, snapshots


def reference():
    raw = b'<map><string name="synthetic">fixture</string></map>'
    snapshot = {"bytes_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()}
    encrypted = base64.b64encode(b"x" * 32).decode()
    rows = []
    for store in (0, 1):
        for index, name in enumerate(CASES):
            result = {"store": store, "route": ROUTE}
            if index == 1:
                result.update(status="threw", exception="javax.crypto.BadPaddingException")
            else:
                plain = b"p" * (32 if index == 2 else 16)
                result.update(status="returned", output=base64.b64encode(plain).decode(), bytes=len(plain))
            rows.append({"store": store, "case": name,
                         "input": base64.b64encode(bytes(16)).decode() if index == 1 else encrypted,
                         "result": result, "stream": "not an expected result"})
    return {"schema": "e2.1l-pn-edge-matrix.v1", "restored": True, "replacement": False,
            "restore_receipt": "pn-restored\t2", "xml_before": [snapshot, snapshot],
            "xml_restored": [snapshot, snapshot], "direct_cipher": rows}


def test_original_result_only_and_versioned_protocol():
    document = reference()
    capsule = build(document, bytes(32), bytes(16))
    assert capsule[:4] == b"NCS1"
    assert struct.unpack_from("<I", capsule, 52) == (4,)
    assert b"not an expected result" not in capsule
    for row in document["direct_cipher"]:
        del row["stream"]
    assert build(document, bytes(32), bytes(16)) == capsule


def test_xml_byte_order_difference_is_explicit():
    document = reference()
    raw = b'<map>\n<string name="synthetic">fixture</string></map>'
    document["xml_restored"][0] = {"bytes_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()}
    assert snapshots(document) == [False, True]
    assert build(document, bytes(32), bytes(16))


@pytest.mark.parametrize("damage", ["schema", "restore", "replacement", "receipt", "xml_digest",
    "xml_value", "xml_duplicate", "rows", "store", "store_bool", "order", "route", "result_store",
    "missing_input", "cipher_base64", "empty_input", "oversize", "error", "error_output", "returned", "output",
    "length", "repeat_input", "repeat_output", "key", "iv"])
def test_unknown_or_inconsistent_history_rejected(damage):
    document = deepcopy(reference())
    row = document["direct_cipher"][4]
    key, iv = bytes(32), bytes(16)
    if damage == "schema": document["schema"] = "unknown"
    elif damage == "restore": document["restored"] = False
    elif damage == "replacement": document["replacement"] = True
    elif damage == "receipt": document["restore_receipt"] = "unknown"
    elif damage == "xml_digest": document["xml_before"][0]["sha256"] = "unknown"
    elif damage in {"xml_value", "xml_duplicate"}:
        raw = b'<map><string name="synthetic">changed</string></map>' if damage == "xml_value" else (
            b'<map><string name="synthetic">fixture</string><string name="synthetic">fixture</string></map>')
        document["xml_restored"][0] = {"bytes_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()}
    elif damage == "rows": document["direct_cipher"].pop()
    elif damage == "store": row["store"] = 0
    elif damage == "store_bool": row["store"] = True
    elif damage == "order": row["case"] = "unknown"
    elif damage == "route": row["result"]["route"] = "candidate"
    elif damage == "result_store": row["result"]["store"] = True
    elif damage == "missing_input": del row["input"]
    elif damage == "cipher_base64": row["input"] = "?"
    elif damage == "empty_input": row["input"] = ""
    elif damage == "oversize": row["input"] = base64.b64encode(bytes(4113)).decode()
    elif damage == "error": document["direct_cipher"][5]["result"]["exception"] = "unknown"
    elif damage == "error_output": document["direct_cipher"][5]["result"]["output"] = ""
    elif damage == "returned": row["result"]["status"] = "threw"
    elif damage == "output": row["result"]["output"] = "?"
    elif damage == "length": row["result"]["bytes"] = 32
    elif damage == "repeat_input": row["input"] = base64.b64encode(b"y" * 32).decode()
    elif damage == "repeat_output": row["result"]["output"] = base64.b64encode(b"q" * 16).decode()
    elif damage == "key": key = bytes(31)
    elif damage == "iv": iv = bytes(15)
    with pytest.raises(ValueError):
        build(document, key, iv)


def test_cli_checks_identities_exclusive_output_and_private_material(tmp_path):
    command = [sys.executable, str(Path(__file__).with_name("preference_sequence_capsule.py"))]
    for name, raw in {"reference": json.dumps(reference()).encode(), "observer": b"synthetic observer",
                      "matrix-source": b"synthetic matrix", "key": bytes(32), "iv": bytes(16)}.items():
        path = tmp_path / name
        path.write_bytes(raw)
        command.extend(["--" + name, str(path), "--" + name + "-sha256", hashlib.sha256(raw).hexdigest()])
    output = tmp_path / "private-output"
    command.extend(["--output", str(output)])
    subprocess.run(command, check=True, capture_output=True)
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "sequence.bin").stat().st_mode & 0o777 == 0o600
    original = (output / "sequence.bin").read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (output / "sequence.bin").read_bytes() == original
    (tmp_path / "observer").write_bytes(b"changed")
    command[-1] = str(tmp_path / "mismatched-output")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert not Path(command[-1]).exists()
