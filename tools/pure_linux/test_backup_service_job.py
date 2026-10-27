import pytest

from backup_service_job import classify


def output(discovery):
    phases = ("before_binder_load", "after_binder_load",
              "before_check_service", "after_check_service")
    return "".join("BACKUP_PHASE\t" + phase + "\n" for phase in phases) + \
        "BACKUP_DISCOVERY\t" + discovery + "\n"


@pytest.mark.parametrize("exit_code,timed_out,discovery,expected", [
    (93, False, "service_absent_not_contract_success", "SERVICE_ABSENT"),
    (94, False, "service_found_contract_not_exercised", "SERVICE_FOUND"),
    (0, False, "service_absent_not_contract_success", "UNRESOLVED"),
    (-6, False, "service_absent_not_contract_success", "UNRESOLVED"),
    (93, True, "service_absent_not_contract_success", "UNRESOLVED"),
    (93, False, "service_found_contract_not_exercised", "UNRESOLVED"),
])
def test_discovery_never_implies_backup_contract_pass(exit_code, timed_out, discovery, expected):
    result = classify(exit_code, timed_out, output(discovery))
    assert result["lookup_result"] == expected
    assert result["backup_contract"] == "UNRESOLVED"
    assert result["status"] == "BLOCKED"
    assert result["catalog_ready_pass"] is False


def test_missing_or_duplicate_phase_is_unknown():
    stdout = output("service_absent_not_contract_success")
    assert classify(93, False, stdout.replace("BACKUP_PHASE\tafter_binder_load\n", ""))["lookup_result"] == "UNRESOLVED"
    assert classify(93, False, stdout + stdout)["lookup_result"] == "UNRESOLVED"


def test_abort_before_lookup_return_is_not_service_absence():
    stdout = "BACKUP_PHASE\tbefore_binder_load\nBACKUP_PHASE\tafter_binder_load\nBACKUP_PHASE\tbefore_check_service\n"
    result = classify(-6, False, stdout)
    assert result["lookup_result"] == "UNRESOLVED"
    assert result["libg_lifecycle_exercised"] is False
