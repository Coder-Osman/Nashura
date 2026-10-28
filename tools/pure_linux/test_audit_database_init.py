import pytest

from audit_database_init import check_chain


def fixture():
    return {"trace": {"activeDbCtorScopes": 0, "activeDbInitScopes": 0, "events": [
        {"kind": "database_open", "parent": 3, "path": "assets.scdb", "result_nonnull": True, "sequence": 1, "thread": 7},
        {"kind": "database_constructor", "id": 3, "path": "assets.scdb", "reader_nonnull": True, "sequence": 2, "thread": 7},
        {"kind": "database_init_enter", "id": 4, "constructor_id": 3, "sequence": 3, "thread": 7},
        {"kind": "database_init_return", "id": 4, "result": 1, "sequence": 4, "thread": 7},
        {"kind": "lookup_inner", "sequence": 5, "tree": {"source_db_init": {"4": 9}}}]}}


def test_requires_matching_constructor_and_init_scope():
    assert check_chain(fixture(), 9)["source_calls_in_scope"] == 9


@pytest.mark.parametrize("damage", ["path", "constructor", "thread", "calls", "open"])
def test_rejects_unbound_or_incomplete_chain(damage):
    data = fixture()
    events = data["trace"]["events"]
    if damage == "path":
        events[0]["path"] = "different"
    elif damage == "constructor":
        events[2]["constructor_id"] = 8
    elif damage == "thread":
        events[3]["thread"] = 8
    elif damage == "calls":
        events[4]["tree"]["source_db_init"]["4"] = 8
    else:
        events[0]["result_nonnull"] = False
    with pytest.raises(ValueError):
        check_chain(data, 9)
