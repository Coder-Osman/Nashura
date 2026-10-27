from copy import deepcopy

import pytest

from preference_reference_job import validate
from test_preference_cipher_job import fixture


def reference():
    _, mapped, identities = fixture()
    return {"status": "PASS", "records": 2, "libg_loaded": False,
            "scope": "explicit_private_reference_bytes_only"}, mapped, identities


def test_private_reference_gate():
    assert validate(*reference(), 2)


@pytest.mark.parametrize("damage", ["null", "count", "extra", "scope", "libg", "mapping", "empty"])
def test_reference_gate_rejects_unproven_evidence(damage):
    summary, mapped, identities = deepcopy(reference())
    if damage == "null": summary = None
    elif damage == "count": summary["records"] = 1
    elif damage == "extra": summary["full_parity"] = True
    elif damage == "scope": summary["scope"] = "full_android"
    elif damage == "libg": summary["libg_loaded"] = True
    elif damage == "mapping": mapped[-1]["sha256"] = "different"
    elif damage == "empty": mapped = []
    assert not validate(summary, mapped, identities, 2)
