import copy

import pytest

import audit_allocator_origin as audit


def test_allocator_origin_rejects_unknown_or_unlinked_evidence(monkeypatch):
    monkeypatch.setattr(audit, "compare_factory", lambda data: None)
    ctor = {"id": 2, "caller": "0x139dd04", "argument_text": "AndroidMainAllocator",
            "argument_nonnull": True, "field_matches_argument": True, "vtable_matches": True, "thread": 4}
    event = {"kind": "owner_allocator_constructor_return", **ctor, "sequence": 1}
    factory = {"kind": "owner_holder_factory_return", "allocator_constructor": ctor,
               "factory_argument_nonnull": True, "factory_argument_matches_field": True, "thread": 5, "sequence": 2}
    data = {"trace": {"events": [event, factory], "coverage": [
        {"label": "owner_allocator_constructor_candidate", "installed": True}]}}
    assert audit.compare(data)["constructor_and_factory_same_thread"] is False
    for damage in ("coverage", "name", "id", "field", "order"):
        changed = copy.deepcopy(data)
        if damage == "coverage": changed["trace"]["coverage"][0]["installed"] = False
        elif damage == "name": changed["trace"]["events"][1]["allocator_constructor"]["argument_text"] = "unknown"
        elif damage == "id": changed["trace"]["events"][0]["id"] = 3
        elif damage == "field": changed["trace"]["events"][1]["factory_argument_matches_field"] = False
        else: changed["trace"]["events"][0]["sequence"] = 3
        with pytest.raises(ValueError):
            audit.compare(changed)
