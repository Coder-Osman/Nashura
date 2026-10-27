from copy import deepcopy
import hashlib

import pytest

from preference_historical_identity import historical_key
from preference_reference_capsule import build
from test_preference_reference_capsule import reference


def observed():
    return {"schema": "e2.1j-derivation-matrix.v1", "restored": True, "cleanup_errors": [],
            "initialization_replaced": False, "original_xml_writer_replaced": False,
            "source_acquisition": "Java retained; explicit transfer to native after complete trajectory",
            "source": {"source": "original-app-context Settings.Secure", "identity": "0123456789abcdef",
                       "package": "fixture.example"}}


def test_historical_source_only_never_profile_key_or_native_field():
    document = observed()
    document.update(profile={"keys": ["unused"]}, guest={"keys": "unused"})
    assert historical_key(document) == hashlib.sha256(b"0123456789abcdef").digest()
    document["profile"]["keys"] = ["different"]
    assert historical_key(document) == historical_key(observed())


@pytest.mark.parametrize("damage", ["schema", "restored", "cleanup", "replacement", "writer",
    "acquisition", "missing", "source", "extra", "identity", "upper", "short", "null", "package"])
def test_unknown_source_rejected(damage):
    document = deepcopy(observed())
    if damage == "schema": document["schema"] = "unknown"
    elif damage == "restored": document["restored"] = False
    elif damage == "cleanup": document["cleanup_errors"] = ["unknown"]
    elif damage == "replacement": document["initialization_replaced"] = True
    elif damage == "writer": document["original_xml_writer_replaced"] = True
    elif damage == "acquisition": document["source_acquisition"] = "unknown"
    elif damage == "missing": document.pop("source")
    elif damage == "source": document["source"]["source"] = "candidate"
    elif damage == "extra": document["source"]["fallback"] = True
    elif damage == "identity": document["source"]["identity"] = "x" * 16
    elif damage == "upper": document["source"]["identity"] = "0123456789ABCDEF"
    elif damage == "short": document["source"]["identity"] = "0" * 15
    elif damage == "null": document["source"]["identity"] = None
    elif damage == "package": document["source"]["package"] = "fixture/path"
    with pytest.raises(ValueError):
        historical_key(document)


def test_explicit_store_selects_distinct_original_reference():
    document = reference()
    assert build(document, bytes(32), bytes(16)) == build(document, bytes(32), bytes(16), 1)
    assert build(document, bytes(32), bytes(16), 0)[68:84] == bytes([7]) * 16
    assert build(document, bytes(32), bytes(16), 1)[68:84] == bytes([8]) * 16


@pytest.mark.parametrize("store", [True, False, None, -1, 2, "0"])
def test_invalid_store_is_not_coerced(store):
    with pytest.raises(ValueError):
        build(reference(), bytes(32), bytes(16), store)
