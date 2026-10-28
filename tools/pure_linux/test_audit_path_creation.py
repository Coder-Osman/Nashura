import pytest

from audit_path_creation import summarize


def test_aggregates_without_exposing_paths():
    rows = [{"caller": "0x10", "bytes_hex": b"a/b".hex(), "calls": 3, "returns": 3},
            {"caller": "0x10", "bytes_hex": b"a/c".hex(), "calls": 2, "returns": 2}]
    result = summarize(rows, b"a/b")
    assert result["calls"] == 5 and result["matching_first_component_calls"] == 5
    assert result["exact_lookup_key_calls"] == 3 and "a/b" not in str(result)


@pytest.mark.parametrize("row", [
    {"caller": "x", "bytes_hex": "ff", "calls": 2, "returns": 1},
    {"caller": "x", "bytes_hex": "FF", "calls": 1, "returns": 1},
    {"caller": "x", "bytes_hex": "zz", "calls": 1, "returns": 1},
    {"caller": "x", "bytes_hex": "00", "calls": True, "returns": 1},
])
def test_rejects_incomplete_or_malformed_history(row):
    with pytest.raises(ValueError):
        summarize([row], b"a")


def test_duplicate_rows_are_rejected():
    row = {"caller": "x", "bytes_hex": "00", "calls": 1, "returns": 1}
    with pytest.raises(ValueError):
        summarize([row, row], b"a")
