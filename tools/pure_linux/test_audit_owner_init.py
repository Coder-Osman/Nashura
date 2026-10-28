import copy

import pytest

import audit_owner_init as audit


def test_owner_init_requires_scope_and_argument_evidence(monkeypatch):
    monkeypatch.setattr(audit, "compare_owner", lambda data: None)
    entry = {"kind": "engine_owner_init_enter", "id": 1, "caller": "0x13a1c88", "owner_constructor_id": 2,
             "argument_nonnull": True, "argument_readable": True, "argument_first_word_mapped": False,
             "argument_first_word_module": None, "argument_matches_slot_before": True,
             "argument_matches_getter_holder_field": True, "sequence": 1, "thread": 7}
    db = {"kind": "engine_db_init_enter", "id": 3, "owner_constructor": {"id": 2},
          "owner_init_id": 1, "sequence": 2, "thread": 7}
    result = {"trace": {"coverage": [{"label": "engine_owner_init_candidate", "installed": True}],
              "events": [entry, db, {"kind": "engine_db_init_return", "id": 3, "sequence": 3, "thread": 7},
                         {"kind": "engine_owner_init_return", "id": 1, "argument_matches_slot_after": True,
                          "sequence": 4, "thread": 7}]}}
    assert audit.compare(result)["database_init_in_scope"]
    for damage in ("coverage", "holder", "thread", "order", "mapped", "scope"):
        altered = copy.deepcopy(result)
        events = altered["trace"]["events"]
        if damage == "coverage": altered["trace"]["coverage"][0]["installed"] = False
        elif damage == "holder": events[0]["argument_matches_getter_holder_field"] = False
        elif damage == "thread": events[1]["thread"] = 8
        elif damage == "order": events[2]["sequence"] = 5
        elif damage == "mapped": events[0]["argument_first_word_mapped"] = True
        else: events[1]["owner_init_id"] = None
        with pytest.raises(ValueError):
            audit.compare(altered)
