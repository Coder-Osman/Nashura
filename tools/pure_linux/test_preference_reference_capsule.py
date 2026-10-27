import base64
from copy import deepcopy
import hashlib
import struct

import pytest

from preference_reference_capsule import TEXTS, build


def reference():
    snapshot = {"bytes_hex": b"synthetic-xml".hex(),
                "sha256": hashlib.sha256(b"synthetic-xml").hexdigest()}
    profile = {name: {"algorithm": "AES/ECB/PKCS5Padding" if name == "c" else "AES/CBC/PKCS5Padding",
                       "provider": "synthetic", "iv": "" if name == "c" else base64.b64encode(bytes(16)).decode()}
               for name in ("a", "b", "c")}
    return {"schema": "e2.1j-derivation-matrix.v1", "restored": True, "cleanup_errors": [],
            "xml_before": [snapshot, snapshot], "xml_after": [snapshot, snapshot],
            "initialization_replaced": False, "original_xml_writer_replaced": False,
            "cipher_info": [dict(profile, store=index, charset="UTF-8") for index in (0, 1)],
            "cases": [{"store": store, "ordinal": ordinal, "purpose": purpose,
                       "original": base64.b64encode(bytes([store + 7]) *
                           ((len(text.encode()) // 16 + 1) * 16)).decode(), "native": "unused"}
                      for store in (0, 1) for ordinal, text in enumerate(TEXTS) for purpose in ("key", "value")]}


def test_only_original_bytes_and_fixed_known_plaintext_enter_capsule():
    capsule = build(reference(), bytes(32), bytes(16))
    assert capsule[:4] == b"NCR1"
    assert struct.unpack_from("<I", capsule, 52) == (30,)
    assert capsule[68:84] == bytes([8]) * 16
    assert b"unused" not in capsule
    changed = reference()
    for row in changed["cases"]:
        row.pop("native")
    assert build(changed, bytes(32), bytes(16)) == capsule


@pytest.mark.parametrize("damage", ["restore", "cleanup", "xml", "xml_digest", "profile",
    "charset", "iv", "algorithm", "ciphertext", "duplicate", "missing", "ordinal", "replacement"])
def test_untrusted_or_incomplete_history_is_rejected(damage):
    document = deepcopy(reference())
    if damage == "restore": document["restored"] = False
    elif damage == "cleanup": document["cleanup_errors"] = ["failure"]
    elif damage == "xml": document["xml_after"] = []
    elif damage == "xml_digest": document["xml_before"][0]["sha256"] = "wrong"
    elif damage == "profile": document["cipher_info"] = []
    elif damage == "charset": document["cipher_info"][1]["charset"] = "unknown"
    elif damage == "iv": document["cipher_info"][1]["a"]["iv"] = ""
    elif damage == "algorithm": document["cipher_info"][1]["c"]["algorithm"] = "unknown"
    elif damage == "ciphertext": document["cases"][0]["original"] = "?"
    elif damage == "duplicate": document["cases"][1] = document["cases"][0]
    elif damage == "missing": document["cases"].pop()
    elif damage == "ordinal": document["cases"][0]["ordinal"] = 11
    elif damage == "replacement": document["initialization_replaced"] = True
    with pytest.raises(ValueError):
        build(document, bytes(32), bytes(16))
