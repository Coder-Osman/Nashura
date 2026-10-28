import copy

import pytest

import audit_holder_factory as audit


def test_holder_factory_requires_same_thread_identity_and_order(monkeypatch):
    monkeypatch.setattr(audit, "compare_init", lambda data: None)
    events = [{"kind": "owner_holder_factory_return", "id": 1, "thread": 4, "sequence": 1,
               "holder_nonnull": True, "allocator_nonnull": True, "allocator_matches_holder": True,
               "allocator_vtable_module": "libg.so", "allocator_vtable_offset": "0x18bf798"},
              {"kind": "owner_holder_setup_enter", "id": 2, "thread": 4, "sequence": 2,
               "caller": "0x139e330", "factory_id": 1, "slot_before_nonnull": False},
              {"kind": "owner_holder_setup_return", "id": 2, "thread": 4, "sequence": 3, "slot_after_nonnull": True},
              {"kind": "engine_owner_init_enter", "holder_setup_id": 2, "thread": 4, "sequence": 4}]
    data = {"trace": {"events": events, "coverage": [{"label": name, "installed": True} for name in
                     ("owner_holder_factory_candidate", "owner_holder_setup_candidate")]}}
    assert audit.compare(data)["owner_init_argument_matches_setup"]
    for damage in ("coverage", "allocator", "factory", "holder", "thread", "order"):
        altered = copy.deepcopy(data)
        rows = altered["trace"]["events"]
        if damage == "coverage": altered["trace"]["coverage"][0]["installed"] = False
        elif damage == "allocator": rows[0]["allocator_vtable_offset"] = "unknown"
        elif damage == "factory": rows[1]["factory_id"] = None
        elif damage == "holder": rows[3]["holder_setup_id"] = None
        elif damage == "thread": rows[2]["thread"] = 5
        else: rows[3]["sequence"] = 2
        with pytest.raises(ValueError):
            audit.compare(altered)
