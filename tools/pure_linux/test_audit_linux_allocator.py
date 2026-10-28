import copy

import pytest

from audit_linux_allocator import ENGINE, compare


def test_requires_valid_identity_order_and_gate():
    result = {"status": "BLOCKED", "exit_code": 0, "timed_out": False, "allocator_only": True,
              "inputs_before": {"libg.so": ENGINE}, "inputs_unchanged": True, "resources_unchanged": True,
              "auxiliary_unchanged": True, "probe_unchanged": True, "postcheck_errors": [], "catalog_ready_pass": False}
    lines = ["ALLOCATOR_CONSTRUCTOR\t1", "ALLOCATOR_ALLOCATION\t1", "ALLOCATOR_RELEASE\t1",
             "ALLOCATOR_INVALID_REJECTED\t1", "PROVIDER_RESULT\t0"]
    assert compare(result, "\n".join(lines))["status"] == "NATIVE_VERIFIED_LIMITED"
    for damage in ("identity", "gate", "ordering", "duplicate", "missing"):
        changed, output = copy.deepcopy(result), list(lines)
        if damage == "identity": changed["inputs_before"]["libg.so"] = "wrong"
        elif damage == "gate": changed["catalog_ready_pass"] = True
        elif damage == "ordering": output[0], output[1] = output[1], output[0]
        elif damage == "duplicate": output.append(lines[0])
        else: output.pop(2)
        with pytest.raises(ValueError):
            compare(changed, "\n".join(output))
