import json

import pytest

from preference_hook_evidence import observations


def logs():
    summary = {"result": -1, "deleted": False, "commits": 2, "disk_writes": 2,
               "false_commits": 0, "backup_requests": 1, "backup_failures": 1,
               "decode_exceptions": 0, "entries": [12, 12]}
    stdout = ("PREFERENCE_BACKEND_HOOK\thistorical_identity_ascii_xml_only_backup_unresolved\n"
              "PREFERENCE_BACKEND_WRITE\t" + json.dumps(summary))
    stderr = ("PREFERENCE_BACKUP\tUNRESOLVED\tTID\t123\n"
              "PREFERENCE_BACKEND_FATAL\tbackup service unresolved after persistence")
    return stdout, stderr


def test_bounded_observation_keeps_strict_gates_closed():
    result = observations(*logs())
    assert result["persistence_before_unresolved_backup_observed"]
    assert not result["configuration_contract_pass"]
    assert not result["catalog_ready_pass"]


@pytest.mark.parametrize("old,new", [
    ('"commits": 2', '"commits": 0'),
    ('"false_commits": 0', '"false_commits": false'),
    ('"entries": [12, 12]', '"entries": [12.0, 12]'),
    ('"backup_failures": 1', '"backup_failures": 0'),
    ("PREFERENCE_BACKEND_WRITE\t", "PREFERENCE_BACKEND_WRITE\tinvalid"),
    ("PREFERENCE_BACKEND_HOOK\t", "MISSING\t"),
    ("TID\t123", "TID\t0"),
    ("PREFERENCE_BACKUP\t", "MISSING\t"),
    ("PREFERENCE_BACKEND_FATAL\t", "MISSING\t"),
])
def test_missing_damaged_or_unexpected_evidence_stays_unknown(old, new):
    stdout, stderr = logs()
    assert not observations(stdout.replace(old, new), stderr.replace(old, new))[
        "persistence_before_unresolved_backup_observed"]


def test_duplicate_evidence_rejected():
    stdout, stderr = logs()
    assert not observations(stdout + "\n" + stdout, stderr)["persistence_before_unresolved_backup_observed"]
    assert not observations(stdout, stderr + "\n" + stderr)["persistence_before_unresolved_backup_observed"]
    assert not observations("", "")["persistence_before_unresolved_backup_observed"]
