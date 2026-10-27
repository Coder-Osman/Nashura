"""Bind a private store1 failure sequence to reviewed original Cipher observations."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ElementTree

from preference_identity_material import private_write


CASES = ("valid-before", "bad-padding", "valid-after-padding", "valid-after-padding-again")
ROUTE = "explicit original store Cipher.b.doFinal; mutates decoder state"


def snapshots(document):
    result = []
    for name in ("xml_before", "xml_restored"):
        rows = document.get(name)
        if not isinstance(rows, list) or len(rows) != 2:
            raise ValueError("two historical XML snapshots required")
        stores = []
        for row in rows:
            raw = bytes.fromhex(row["bytes_hex"])
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ValueError("historical XML digest differs")
            root = ElementTree.fromstring(raw)
            values = {}
            if root.tag != "map":
                raise ValueError("historical preferences map required")
            for entry in root:
                key = entry.get("name")
                if not key or key in values:
                    raise ValueError("duplicate or unnamed historical preference")
                values[key] = ElementTree.tostring(entry)
            stores.append(values)
        result.append(stores)
    if result[0] != result[1]:
        raise ValueError("historical preference entries not restored")
    return [left["sha256"] == right["sha256"] for left, right in
            zip(document["xml_before"], document["xml_restored"])]


def build(document, key, iv):
    if len(key) != 32 or len(iv) != 16:
        raise ValueError("explicit private AES256 key and IV required")
    if (document.get("schema") != "e2.1l-pn-edge-matrix.v1" or
            document.get("restored") is not True or document.get("replacement") is not False or
            document.get("restore_receipt") != "pn-restored\t2"):
        raise ValueError("reviewed original restored history required")
    snapshots(document)
    rows = document.get("direct_cipher")
    if (not isinstance(rows, list) or len(rows) != 8 or
            [row.get("store") for row in rows] != [0] * 4 + [1] * 4 or
            any(type(row.get("store")) is not int for row in rows)):
        raise ValueError("complete ordered original two-store sequence required")
    selected = rows[4:]
    if tuple(row.get("case") for row in selected) != CASES:
        raise ValueError("reviewed store1 order required")
    records = []
    for index, row in enumerate(selected):
        result = row["result"]
        if (result.get("store") != 1 or type(result.get("store")) is not int or
                result.get("route") != ROUTE):
            raise ValueError("explicit original store1 Cipher route required")
        if not isinstance(row.get("input"), str):
            raise ValueError("recorded original ciphertext required; reconstruction prohibited")
        ciphertext = base64.b64decode(row["input"], validate=True)
        if not ciphertext or len(ciphertext) > 4112:
            raise ValueError("bounded original ciphertext required")
        if index == 1:
            if (ciphertext != bytes(16) or result.get("status") != "threw" or
                    result.get("exception") != "javax.crypto.BadPaddingException" or
                    "output" in result or "bytes" in result):
                raise ValueError("reviewed original padding exception required")
            expected, failed = b"", 1
        else:
            if result.get("status") != "returned" or "exception" in result:
                raise ValueError("original successful return required")
            expected = base64.b64decode(result["output"], validate=True)
            expected_size = 32 if index == 2 else 16
            if (type(result.get("bytes")) is not int or
                    len(expected) != result["bytes"] or len(expected) != expected_size):
                raise ValueError("original result length differs")
            failed = 0
        records.append(struct.pack("<III", failed, len(ciphertext), len(expected)) + ciphertext + expected)
    if (selected[0]["input"] != selected[2]["input"] or
            selected[0]["input"] != selected[3]["input"] or
            selected[0]["result"]["output"] != selected[3]["result"]["output"]):
        raise ValueError("reviewed repeated ciphertext sequence differs")
    return b"NCS1" + key + iv + struct.pack("<I", 4) + b"".join(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("reference", "observer", "matrix-source", "key", "iv")
    for name in names:
        parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    inputs = {}
    for name in names:
        path = getattr(args, name.replace("-", "_"))
        inputs[name] = path.read_bytes()
        if hashlib.sha256(inputs[name]).hexdigest() != getattr(args, name.replace("-", "_") + "_sha256"):
            raise ValueError("reviewed private input identity differs")
    document = json.loads(inputs["reference"])
    capsule = build(document, inputs["key"], inputs["iv"])
    if any(getattr(args, name.replace("-", "_")).read_bytes() != raw for name, raw in inputs.items()):
        raise ValueError("private input changed")
    args.output.mkdir(mode=0o700, exist_ok=False)
    private_write(args.output / "sequence.bin", capsule)
    summary = {"schema": "nashura.preference-sequence-capsule.v1", "records": 4,
               "reference_sha256": args.reference_sha256, "observer_sha256": args.observer_sha256,
               "matrix_source_sha256": args.matrix_source_sha256,
               "capsule_sha256": hashlib.sha256(capsule).hexdigest(), "inputs_unchanged": True,
               "xml_bytes_restored": snapshots(document), "xml_entries_restored": True,
               "source": "original result only; stream/native fields never used",
               "scope": "historical store1 padding failure and following return bytes only",
               "exception_type_equivalence": "UNRESOLVED", "current_runtime_binding": "UNRESOLVED"}
    private_write(args.output / "result.json", (json.dumps(summary, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
