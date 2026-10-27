import json

import pytest

from preference_cipher_material import extract
from preference_method_audit import METHODS, sha256


def fixture(value="0123456789abcdefsynthetic"):
    return ("001000: |[001000] " + METHODS[0] + "\n001010: |0000: const-string v5, " +
            json.dumps(value) + " // string@0001\n      positions     :\n")


def test_extracts_only_selected_fingerprinted_literal_with_ascii_truncation():
    text = fixture()
    assert extract(text, sha256(b"0123456789abcdefsynthetic")) == b"0123456789abcdef"


@pytest.mark.parametrize("text,value", [
    (fixture(), b"different"), (fixture() + fixture(), b"0123456789abcdefsynthetic"),
    (fixture("short"), b"short"), (fixture("\u00e9" * 20), ("\u00e9" * 20).encode()),
    (fixture().replace("const-string v5", "const-string v?"), b"0123456789abcdefsynthetic"),
])
def test_missing_ambiguous_short_non_ascii_or_unparsed_literal_fails(text, value):
    with pytest.raises(ValueError):
        extract(text, sha256(value))
