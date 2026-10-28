import copy

import pytest

from audit_database_factory import compare_factory


def fixture():
    names = ("database_factory_candidate", "engine_db_init_candidate", "libc_open", "libc_openat")
    return {"trace": {"activeDbFactoryScopes": 0, "activeEngineDbScopes": 0,
        "coverage": [{"label": n, "installed": True} for n in names], "events": [
            {"kind": "engine_db_init_enter", "id": 1, "caller": "0x1", "thread": 7, "sequence": 1,
             "owner_is_root": False, "owner_is_context": False, "slot_before_nonnull": False},
            {"kind": "database_libc_open", "parent": 3, "thread": 7, "sequence": 2, "api": "open", "fd": 5,
             "path": "/data/user/0/nullsroyale.rel.free/update/assets.scdb"},
            {"kind": "filesystem_open", "database_parent": 3, "thread": 7, "sequence": 3,
             "path": "assets.scdb", "nonnull": True, "vtable": "0x1"},
            {"kind": "database_constructor", "id": 3, "factory_parent": 2, "thread": 7, "sequence": 4},
            {"kind": "database_factory", "id": 2, "engine_parent": 1, "thread": 7, "sequence": 5,
             "caller": "0x2", "result_nonnull": True},
            {"kind": "engine_db_init_return", "id": 1, "thread": 7, "sequence": 6, "slot_after_nonnull": True}]}}


def test_tracks_scoped_successful_open():
    assert compare_factory(fixture())["physical_update_path_open_count"] == 1


@pytest.mark.parametrize("damage", ["fd", "path", "thread", "factory", "coverage"])
def test_rejects_partial_observation(damage):
    data = copy.deepcopy(fixture())
    events = data["trace"]["events"]
    if damage == "fd": events[1]["fd"] = -1
    elif damage == "path": events[1]["path"] = "/tmp/assets.scdb"
    elif damage == "thread": events[1]["thread"] = 8
    elif damage == "factory": events[3]["factory_parent"] = 9
    else: data["trace"]["coverage"][0]["installed"] = False
    with pytest.raises(ValueError):
        compare_factory(data)
