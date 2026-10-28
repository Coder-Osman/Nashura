import copy

import pytest

import audit_owner_constructor as audit


def test_rejects_missing_or_mismatched_owner_link(monkeypatch):
    monkeypatch.setattr(audit, "compare_owner", lambda data: None)
    monkeypatch.setattr(audit, "compare_factory", lambda data: None)
    getter = {"id": 1, "caller": "0x13a43e0", "thread": 7}
    event = {"kind": "engine_db_init_enter", "thread": 7, "owner_vtable": "0x17ea568",
             "owner_getter": getter, "owner_constructor": {"caller": "0x755804", "thread": 7,
             "returned": True, "vtable_matches": True, "prior_address_observations": 0,
             "getter_parent": dict(getter)}}
    data = {"status": "RUNTIME_OBSERVED_REQUIRES_AUDIT", "engine_sha256": audit.ENGINE_SHA,
            "script_error_count": 0, "update_unchanged": True, "update_before": {"count": 908},
            "update_after": {"count": 908}, "trace": {"errors": [], "lost": 0, "events": [event],
            "coverage": [{"label": name, "installed": True} for name in
                         ("engine_owner_getter_candidate", "engine_owner_constructor_candidate", "engine_db_init_candidate")]}}
    assert audit.compare(data)["same_thread"] is True
    for damage in ("missing", "thread", "reused", "unreturned", "dropped", "inventory", "coverage"):
        altered = copy.deepcopy(data)
        owner = altered["trace"]["events"][0]
        if damage == "missing": owner["owner_getter"] = None
        elif damage == "thread": owner["owner_constructor"]["thread"] = 8
        elif damage == "reused": owner["owner_constructor"]["prior_address_observations"] = 1
        elif damage == "unreturned": owner["owner_constructor"]["returned"] = False
        elif damage == "dropped": altered["trace"]["lost"] = 1
        elif damage == "inventory": altered["update_after"]["count"] = 907
        else: altered["trace"]["coverage"][0]["installed"] = False
        with pytest.raises(ValueError):
            audit.compare(altered)
