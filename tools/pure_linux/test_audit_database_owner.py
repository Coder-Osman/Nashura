import pytest

from audit_database_owner import compare_owner


def fixture():
    return {"trace": {"events": [
        {"kind": "engine_db_init_enter", "sequence": 1, "root_nonnull": False, "context_nonnull": False,
         "owner_vtable": "0x1", "owner_references": {"root": None, "context": None, "globals": []}},
        {"kind": "batch_phase", "phase": "finish_a", "edge": "before", "sequence": 2,
         "later_root_nonnull": True, "later_context_nonnull": True,
         "owner_matches_later_root": False, "owner_matches_later_context": False}]}}


def test_early_null_is_not_reported_as_a_completed_reference_scan():
    assert compare_owner(fixture())["root_reference_scan"] == "NOT_COVERED"


@pytest.mark.parametrize("damage", ["early", "late", "refs", "order"])
def test_rejects_incomplete_observation(damage):
    data = fixture()
    first, later = data["trace"]["events"]
    if damage == "early": first["root_nonnull"] = True
    elif damage == "late": later["later_context_nonnull"] = False
    elif damage == "refs": first["owner_references"]["root"] = []
    else: later["sequence"] = 0
    with pytest.raises(ValueError):
        compare_owner(data)
