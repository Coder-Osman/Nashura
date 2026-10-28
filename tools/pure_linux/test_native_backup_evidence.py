import json

import pytest

from preference_hook_evidence import native_queue_observations, observations


def logs():
    queue = {"result": 0, "created": True, "delivery": "PENDING", "android_parity": "NOT_VERIFIED"}
    summary = {"result": 0, "deleted": False, "commits": 2, "disk_writes": 2,
               "false_commits": 0, "backup_requests": 1, "backup_failures": 0,
               "decode_exceptions": 0, "entries": [12, 12]}
    return ("PREFERENCE_BACKEND_HOOK\thistorical_ascii_native_backup_candidate_parity_unverified\n"
            "PREFERENCE_NATIVE_BACKUP_QUEUE\t" + json.dumps(queue) + "\n"
            "PREFERENCE_BACKEND_WRITE\t" + json.dumps(summary))


def test_enqueue_observation_never_claims_delivery_or_strict_parity():
    result = native_queue_observations(logs(), "")
    assert result["bounded_enqueue_observed"]
    assert result["delivery"] == "PENDING"
    assert result["android_backup_parity"] == "NOT_VERIFIED"
    assert not result["configuration_contract_pass"]
    assert not result["catalog_ready_pass"]
    assert not observations(logs(), "")["hook_bound"]


def test_unknown_nonempty_key_membership_cannot_validate_enqueue():
    result = native_queue_observations(logs(), "", expected_entries=(None, None))
    assert result["write_summary"]["entries"] == [12, 12]
    assert not result["bounded_enqueue_observed"]


@pytest.mark.parametrize("old,new", [
    ('"result": 0', '"result": false'),
    ('"commits": 2', '"commits": 0'),
    ('"created": true', '"created": 1'),
    ('"entries": [12, 12]', '"entries": [12.0, 12]'),
    ('"PENDING"', '"DELIVERED"'),
    ('"NOT_VERIFIED"', '"VERIFIED"'),
    ('"backup_failures": 0', '"backup_failures": 1'),
    ("PREFERENCE_BACKEND_HOOK", "OTHER"),
    ("PREFERENCE_NATIVE_BACKUP_QUEUE", "OTHER"),
    ("PREFERENCE_BACKEND_WRITE", "OTHER"),
])
def test_damaged_or_overclaimed_evidence_stays_unknown(old, new):
    result = native_queue_observations(logs().replace(old, new), "")
    assert not result["bounded_enqueue_observed"]
    assert result["delivery"] == "UNKNOWN"


@pytest.mark.parametrize("stderr", ["PREFERENCE_BACKEND_FATAL\tfailure", "PREFERENCE_BACKUP\tUNRESOLVED",
                                    "PREFERENCE_DECODE_EXCEPTION\tUNRESOLVED"])
def test_failures_are_never_ignored(stderr):
    assert not native_queue_observations(logs(), stderr)["bounded_enqueue_observed"]


def test_missing_duplicate_and_reordered_evidence_stays_unknown():
    assert not native_queue_observations("", "")["bounded_enqueue_observed"]
    assert not native_queue_observations(logs() + "\n" + logs(), "")["bounded_enqueue_observed"]
    assert not native_queue_observations("\n".join(reversed(logs().splitlines())), "")["bounded_enqueue_observed"]
