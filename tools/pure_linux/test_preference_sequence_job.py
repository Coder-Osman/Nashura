from copy import deepcopy

import pytest

from preference_sequence_job import validate
from test_preference_cipher_job import fixture


def reference():
    _, mapped, identities = fixture()
    rows = [{"result": result, "length": length, "expected_length": length,
             "matched": True, "output_guard": True}
            for result, length in ((0, 16), (-3, 0), (0, 32), (0, 16))]
    return {"status": "PASS", "records": 4, "libg_loaded": False,
            "scope": "historical_store1_padding_sequence_bytes_only", "rows": rows}, mapped, identities


def test_bounded_sequence_gate():
    assert validate(*reference(), 4)


@pytest.mark.parametrize("damage", ["missing", "status", "count", "scope", "libg", "bytes",
    "error", "guard", "matched", "bool", "mapping", "empty_mapping", "extra", "expected_count"])
def test_unproven_sequence_rejected(damage):
    document, mapped, identities = deepcopy(reference())
    count = 4
    if damage == "missing": document = None
    elif damage == "status": document["status"] = "BLOCKED"
    elif damage == "count": document["records"] = 3
    elif damage == "scope": document["scope"] = "full_android"
    elif damage == "libg": document["libg_loaded"] = True
    elif damage == "bytes": document["rows"][2]["length"] = 16
    elif damage == "error": document["rows"][1]["result"] = -1
    elif damage == "guard": document["rows"][1]["output_guard"] = False
    elif damage == "matched": document["rows"][2]["matched"] = False
    elif damage == "bool": document["rows"][0]["result"] = False
    elif damage == "mapping": mapped[-1]["sha256"] = "different"
    elif damage == "empty_mapping": mapped = []
    elif damage == "extra": document["no_vm"] = True
    elif damage == "expected_count": count = 3
    assert not validate(document, mapped, identities, count)
