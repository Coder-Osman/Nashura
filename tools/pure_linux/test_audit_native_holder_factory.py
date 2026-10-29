import copy

import pytest

import audit_native_holder_factory as audit


def test_audit_requires_source_identity_and_complete_cleanup(monkeypatch):
    monkeypatch.setattr(audit, "compare_allocator", lambda data: None)
    source = {"id": 1, "caller": "0x139dd10", "thread": 3, "wrapper_nonnull": True,
              "argument_nonnull": True, "allocator_matches": True}
    factory = {"kind": "owner_holder_factory_return", "sequence": 2, "factory_argument_source": source,
               "allocator_constructor": {"thread": 3}, "factory_argument_matches_parent_field": True,
               "factory_argument_table_module": "libg.so", "factory_argument_table_offset": "0x18c8b40"}
    android = {"trace": {"coverage": [{"label": "owner_factory_argument_source_candidate", "installed": True}],
               "events": [{"kind": "owner_factory_argument_source_return", "id": 1, "sequence": 1}, factory]}}
    linux = {"status": "BLOCKED", "exit_code": 0, "timed_out": False, "holder_factory_only": True,
             "inputs_before": {"libg.so": audit.ENGINE}, "inputs_unchanged": True, "resources_unchanged": True,
             "auxiliary_unchanged": True, "probe_unchanged": True, "postcheck_errors": [], "catalog_ready_pass": False}
    lines = ["HOLDER_ARGUMENT_CREATED\t1", "HOLDER_FACTORY_CREATED\t1", "HOLDER_FACTORY_RELEASED\t1", "PROVIDER_RESULT\t0"]
    assert audit.compare(android, linux, "\n".join(lines))["linux_native_cleanup_returned"]
    for damage in ("coverage", "source", "order", "identity", "gate", "cleanup"):
        a, l, out = copy.deepcopy(android), copy.deepcopy(linux), list(lines)
        if damage == "coverage": a["trace"]["coverage"][0]["installed"] = False
        elif damage == "source": a["trace"]["events"][1]["factory_argument_source"]["allocator_matches"] = False
        elif damage == "order": a["trace"]["events"][0]["sequence"] = 3
        elif damage == "identity": l["inputs_before"]["libg.so"] = "wrong"
        elif damage == "gate": l["catalog_ready_pass"] = True
        else: out.pop(2)
        with pytest.raises(ValueError):
            audit.compare(a, l, "\n".join(out))
