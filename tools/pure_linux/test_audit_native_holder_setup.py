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
    linux = {"status": "HOLDER_SETUP_OBSERVED", "exit_code": 0, "timed_out": False,
             "holder_setup_only": True, "inputs_before": {"libg.so": audit.ENGINE},
             "inputs_unchanged": True, "resources_unchanged": True,
             "auxiliary_unchanged": True, "probe_unchanged": True,
             "postcheck_errors": [], "catalog_ready_pass": False}
    lines = ["HOLDER_SETUP_INPUT_READY\t1", "HOLDER_SETUP_SLOT_BEFORE\t0", "HOLDER_SETUP_NATIVE_RETURN\t1",
             "HOLDER_SETUP_SLOT_AFTER\t1", "HOLDER_SETUP_ALLOCATION_RANGE\t1",
             "HOLDER_SETUP_PAYLOAD_RANGE\t1", "HOLDER_SETUP_ALLOCATOR_MATCH\t1",
             "HOLDER_SETUP_ALLOCATION_SELF_MATCH\t1", "HOLDER_SETUP_OBJECT_OFFSET\t24",
             "HOLDER_SETUP_RETURNED\t1", "HOLDER_SETUP_FILESYSTEM_PRESENT\t1",
             "HOLDER_SETUP_FILESYSTEM_REPLACED\t1", "PROVIDER_STRING\tdata\t8\t0\t",
             "PROVIDER_STRING\tupdate\t8\t0\t", "HOLDER_SETUP_SLOT_CLEARED\t1",
             "HOLDER_SETUP_FILESYSTEM_RETAINED\t1", "HOLDER_SETUP_RELEASED\t1",
             "PROVIDER_RESULT\t0"]
    return android, linux, "\n".join(lines)


def test_setup_audit_accepts_bounded_lifecycle():
    android, linux, stdout = fixture_data()
    result = audit.compare(android, linux, stdout)
    assert result["linux_setup_object_verified"] is True
    assert result["catalog_ready_pass"] is False
    assert result["holder_setup_semantics_pass"] is False
    assert result["setup_provider_path_lengths"] == {"data": 0, "update": 0}


@pytest.mark.parametrize("damage", ["coverage", "slot", "association", "identity", "output", "return_id", "status"])
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
    elif damage == "return_id":
        android["trace"]["events"][1]["id"] = 9
    elif damage == "status":
        linux["status"] = "BLOCKED"
    else:
        stdout = stdout.replace("HOLDER_SETUP_RELEASED\t1", "")
    with pytest.raises(ValueError):
        audit.compare(android, linux, stdout)


@pytest.mark.parametrize("damage", ["offset", "self", "duplicate", "order", "missing_path", "length"])
def test_setup_audit_rejects_wrong_layout_or_provider_observation(damage):
    android, linux, stdout = fixture_data()
    if damage == "offset":
        stdout = stdout.replace("OBJECT_OFFSET\t24", "OBJECT_OFFSET\t0")
    elif damage == "self":
        stdout = stdout.replace("ALLOCATION_SELF_MATCH\t1", "ALLOCATION_SELF_MATCH\t0")
    elif damage == "duplicate":
        stdout += "\nHOLDER_SETUP_RETURNED\t1"
    elif damage == "order":
        stdout = "\n".join(reversed(stdout.splitlines()))
    elif damage == "missing_path":
        stdout = stdout.replace("PROVIDER_STRING\tdata\t8\t0\t", "")
    else:
        stdout = stdout.replace("PROVIDER_STRING\tdata\t8\t0\t", "PROVIDER_STRING\tdata\t8\t1\t")
    with pytest.raises(ValueError):
        audit.compare(android, linux, stdout)


def test_java_exception_does_not_turn_lifecycle_into_semantic_success():
    android, linux, stdout = fixture_data()
    result = audit.compare(android, linux, stdout, "java.lang.NullPointerException: Context")
    assert result["java_exception_reported"] is True
    assert result["holder_setup_semantics_pass"] is False


def test_setup_audit_reads_top_level_inventory_from_runtime_result():
    android, linux, stdout = fixture_data()
    inventory = android["trace"].pop("update_before")
    android["update_before"] = inventory
    android["update_after"] = android["trace"].pop("update_after")
    result = audit.compare(android, linux, stdout)
    assert result["status"] == "NATIVE_VERIFIED_LIMITED"
