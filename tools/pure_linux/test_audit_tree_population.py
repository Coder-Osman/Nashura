import pytest

from audit_tree_population import history


def test_accepts_only_complete_counts():
    row = {"calls": 2, "returns": 2, "callers": {"0x10": 2}}
    assert history(row) == row


@pytest.mark.parametrize("row", [None, {"calls": 0, "returns": 1, "callers": {}},
                                   {"calls": 2, "returns": 2, "callers": {"0x10": 1}},
                                   {"calls": True, "returns": 1, "callers": {"0x10": 1}}])
def test_rejects_unknown_or_partial_history(row):
    with pytest.raises(ValueError):
        history(row)
