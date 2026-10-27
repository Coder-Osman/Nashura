"""Build private normal-path references from reviewed historical original Cipher output."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct

from preference_identity_material import private_write


TEXTS = ["", "a", "b" * 15, "c" * 16, "d" * 17, "中文", "🙂",
         'line\n<>&"', "x" * 255, "z" * 4095]


def build(document, key, iv, store=1):
    if type(store) is not int or store not in (0, 1):
        raise ValueError("explicit historical store0 or store1 required")
    if len(key) != 32 or len(iv) != 16:
        raise ValueError("explicit AES256 key and IV required")
    if (document.get("schema") != "e2.1j-derivation-matrix.v1" or
            document.get("restored") is not True or document.get("cleanup_errors") != [] or
            not document.get("xml_before") or document["xml_before"] != document.get("xml_after") or
            document.get("initialization_replaced") is not False or
            document.get("original_xml_writer_replaced") is not False):
        raise ValueError("reviewed restored historical reference required")
    snapshots = document["xml_before"]
    if not isinstance(snapshots, list) or len(snapshots) != 2:
        raise ValueError("two original XML snapshots required")
    for snapshot in snapshots:
        if hashlib.sha256(bytes.fromhex(snapshot["bytes_hex"])).hexdigest() != snapshot["sha256"]:
            raise ValueError("historical XML digest differs")
    profiles = document.get("cipher_info")
    if not isinstance(profiles, list) or len(profiles) != 2:
        raise ValueError("two original Cipher profiles required")
    for index, profile in enumerate(profiles):
        if (type(profile.get("store")) is not int or profile["store"] != index or
                profile.get("charset") != "UTF-8"):
            raise ValueError("historical store or charset differs")
        for name in ("a", "b", "c"):
            cipher = profile[name]
            expected = "AES/ECB/PKCS5Padding" if name == "c" else "AES/CBC/PKCS5Padding"
            if cipher["algorithm"] != expected or not cipher["provider"]:
                raise ValueError("historical Cipher algorithm differs")
            actual_iv = base64.b64decode(cipher["iv"], validate=True)
            if actual_iv != (b"" if name == "c" else iv):
                raise ValueError("historical Cipher IV differs")
    rows = document.get("cases")
    if not isinstance(rows, list) or len(rows) != 40:
        raise ValueError("complete bounded historical matrix required")
    selected = {}
    for row in rows:
        row_store, ordinal, purpose = row["store"], row["ordinal"], row["purpose"]
        if (type(row_store) is not int or row_store not in (0, 1) or type(ordinal) is not int or
                not 0 <= ordinal < len(TEXTS) or purpose not in ("key", "value")):
            raise ValueError("unknown historical case")
        identity = (row_store, ordinal, purpose)
        if identity in selected:
            raise ValueError("duplicate historical case")
        encrypted = base64.b64decode(row["original"], validate=True)
        size = len(TEXTS[ordinal].encode("utf-8"))
        if len(encrypted) != (size // 16 + 1) * 16:
            raise ValueError("historical ciphertext length differs")
        selected[identity] = encrypted
    records = []
    for ordinal, text in enumerate(TEXTS):
        plain = text.encode("utf-8")
        encoded_key = selected[store, ordinal, "key"]
        encoded_value = selected[store, ordinal, "value"]
        records.extend([(0, plain, encoded_key), (1, plain, encoded_value), (2, encoded_value, plain)])
    capsule = b"NCR1" + key + iv + struct.pack("<I", len(records))
    for operation, input_bytes, expected in records:
        capsule += struct.pack("<III", operation, len(input_bytes), len(expected)) + input_bytes + expected
    return capsule


def main():
    parser = argparse.ArgumentParser()
    names = ("reference", "observer", "matrix-source", "texts-source", "key", "iv")
    for name in names:
        parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--store", type=int, choices=(0, 1), default=1)
    args = parser.parse_args()
    inputs = {}
    for name in names:
        data = getattr(args, name.replace("-", "_")).read_bytes()
        if hashlib.sha256(data).hexdigest() != getattr(args, name.replace("-", "_") + "_sha256"):
            raise ValueError("reviewed private input identity differs")
        inputs[name] = data
    capsule = build(json.loads(inputs["reference"]), inputs["key"], inputs["iv"], args.store)
    if any(getattr(args, name.replace("-", "_")).read_bytes() != data for name, data in inputs.items()):
        raise ValueError("private input changed")
    args.output.mkdir(mode=0o700, exist_ok=False)
    private_write(args.output / "reference.bin", capsule)
    summary = {"schema": "nashura.preference-reference-capsule.v1", "records": 30,
               "reference_sha256": args.reference_sha256, "observer_sha256": args.observer_sha256,
               "matrix_source_sha256": args.matrix_source_sha256, "texts_source_sha256": args.texts_source_sha256,
               "capsule_sha256": hashlib.sha256(capsule).hexdigest(), "inputs_unchanged": True,
               "source": "historical original field; native field never used",
               "store": args.store,
               "scope": "store%d; reviewed normal-path historical Cipher bytes only" % args.store,
               "runtime_binding": "UNRESOLVED", "storage_and_backup": "UNRESOLVED"}
    private_write(args.output / "result.json", (json.dumps(summary, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
