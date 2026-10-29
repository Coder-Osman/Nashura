import copy

import pytest

import audit_native_holder_setup as audit


def fixture_data():
    enter = {"kind": "owner_holder_setup_enter", "id": 2, "caller": "0x139e330",
             "slot_before_nonnull": False, "sequence": 2}
    returned = {"kind": "owner_holder_setup_return", "id": 2, "slot_after_nonnull": True, "sequence": 4}
    init = {"kind": "engine_owner_init_enter", "holder_setup_id": 2,
            "argument_matches_getter_holder_field": True, "sequence": 273}
    android = {"trace": {"errors": [], "lost": 0,
               "coverage": [{"label": "owner_holder_setup_candidate", "installed": True}],
               "update_before": {"count": 908, "sha256": "x"},
               "update_after": {"count": 908, "sha256": "x"},
               "events": [enter, returned, init]}}
    linux = {"status": "BLOCKED", "exit_code": 0, "timed_out": False,
             "holder_setup_only": True, "inputs_before": {"libg.so": audit.ENGINE},
             "inputs_unchanged": True, "resources_unchanged": True,
             "auxiliary_unchanged": True, "probe_unchanged": True,
             "postcheck_errors": [], "catalog_ready_pass": False}
    lines = ["HOLDER_SETUP_INPUT_READY\t1", "HOLDER_SETUP_SLOT_BEFORE\t0",
             "HOLDER_SETUP_SLOT_AFTER\t1", "HOLDER_SETUP_OBJECT_RANGE\t1",
             "HOLDER_SETUP_ALLOCATOR_MATCH\t1", "HOLDER_SETUP_OBJECT_OFFSET\t0",
             "HOLDER_SETUP_RETURNED\t1", "HOLDER_SETUP_SLOT_CLEARED\t1",
             "HOLDER_SETUP_RELEASED\t1", "PROVIDER_RESULT\t0"]
    return android, linux, "\n".join(lines)


def test_setup_audit_accepts_bounded_lifecycle():
    android, linux, stdout = fixture_data()
    result = audit.compare(android, linux, stdout)
    assert result["linux_setup_object_verified"] is True
    assert result["catalog_ready_pass"] is False


@pytest.mark.parametrize("damage", ["coverage", "slot", "association", "identity", "output"])
def test_setup_audit_rejects_damaged_evidence(damage):
    android, linux, stdout = fixture_data()
    if damage == "coverage":
        android["trace"]["coverage"][0]["installed"] = False
    elif damage == "slot":
        android["trace"]["events"][0]["slot_before_nonnull"] = True
    elif damage == "association":
        android["trace"]["events"][2]["holder_setup_id"] = 9
    elif damage == "identity":
        linux["inputs_before"]["libg.so"] = "wrong"
    else:
        stdout = stdout.replace("HOLDER_SETUP_RELEASED\t1", "")
    with pytest.raises(ValueError):
        audit.compare(android, linux, stdout)
