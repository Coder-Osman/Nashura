from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_selection_handoff import handoff, parent
from test_selection_execution import execution, execute, worker, write_request
from test_active_verifier import comparison, series


@pytest.fixture
def retry_parent(parent, monkeypatch, tmp_path):
    failure = tmp_path / "failed-parent"
    failure.mkdir()
    (failure / "failure.json").write_text('{"status":"failed"}')
    proof = parent["verification"]
    proof.update(status="passed_active_retry_chain_and_two_stage_checkpoint_scope",
        parent_status="failed_trainable_timeout_preserved", parent_launch_sha256="1" * 64,
        parent_receipt_sha256="2" * 64, parent_failure_sha256="3" * 64,
        launch_sha256=parent["options"]["launch_sha"], receipt_sha256=parent["options"]["receipt_sha"],
        native_game_provenance_reverified=False,
        verifier_sha256=handoff.sha(Path(handoff.__file__).with_name("verify_active_retry.py")))
    calls = []

    def verify(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(proof)

    monkeypatch.setattr(handoff, "verify_retry", verify)
    parent.update(failure=failure, retry_calls=calls)
    return parent


def prepare_retry(fixture):
    return handoff.prepare_retry(fixture["failure"], fixture["path"],
        parent_launch_sha="1" * 64, parent_receipt_sha="2" * 64,
        output=fixture["output"], **fixture["options"])


def test_retry_handoff_keeps_failed_parent_and_binds_both_archives(retry_parent):
    before = (retry_parent["failure"] / "failure.json").read_bytes()
    report = prepare_retry(retry_parent)
    assert retry_parent["calls"] == []
    assert retry_parent["retry_calls"] == [((retry_parent["failure"], retry_parent["path"]),
        dict(parent_launch_sha="1" * 64, parent_receipt_sha="2" * 64, **retry_parent["options"]))]
    assert report["parent_launch_sha256"] == retry_parent["options"]["launch_sha"]
    assert report["recovery_provenance"]["parent_launch_sha256"] == "1" * 64
    assert report["recovery_provenance"]["parent_status"] == "failed_trainable_timeout_preserved"
    assert (retry_parent["failure"] / "failure.json").read_bytes() == before
    worker.checked_recovery(report, retry_parent["verification"])
    with pytest.raises(ValueError, match="verified native"):
        worker.checked_handoff(retry_parent["output"],
            worker.sha(retry_parent["output"] / "selection-handoff.json"))
    assert not report["training_admissible"] and not report["gates_closed"]


@pytest.mark.parametrize("field,value", (("status", "failed"),
    ("parent_status", "completed"), ("parent_launch_sha256", "4" * 64),
    ("parent_receipt_sha256", "4" * 64), ("launch_sha256", "4" * 64),
    ("receipt_sha256", "4" * 64), ("verifier_sha256", "4" * 64),
    ("native_game_provenance_reverified", True), ("training_admissible", True)))
def test_retry_handoff_rejects_changed_chain_or_scope_before_writing(retry_parent, field, value):
    retry_parent["verification"][field] = value
    with pytest.raises(ValueError):
        prepare_retry(retry_parent)
    assert not retry_parent["output"].exists()


def test_retry_output_must_be_independent_of_both_archives(retry_parent):
    retry_parent["output"] = retry_parent["failure"] / "output"
    with pytest.raises(ValueError, match="independent"):
        prepare_retry(retry_parent)
    assert retry_parent["retry_calls"] == []


@pytest.fixture
def retry_execution(execution):
    path = execution["handoff"] / "parent-verification.json"
    proof = worker.read(path)
    proof.update(status="passed_active_retry_chain_and_two_stage_checkpoint_scope",
        native_game_provenance_reverified=False)
    recovery = dict(schema="FairFinalSelectionRecoveryProvenance/v1",
        parent_status="failed_trainable_timeout_preserved", parent_launch_sha256="1" * 64,
        parent_receipt_sha256="2" * 64, parent_failure_sha256="3" * 64, verifier_sha256="4" * 64)
    proof.update({name: value for name, value in recovery.items() if name != "schema"})
    execution["request"].update(recovery_provenance=recovery, verifier_sha256="4" * 64)
    path.write_text(json.dumps(proof))
    write_request(execution)
    return execution


def test_retry_handoff_runs_real_child_with_full_resume(retry_execution):
    completion = execute(retry_execution)
    assert completion["exit_code"] == 0
    assert completion["training_admissible"] is False
    inputs = retry_execution["options"]["output"] / "inputs"
    proof = worker.read(inputs / "parent-verification.json")
    assert proof["parent_status"] == "failed_trainable_timeout_preserved"


@pytest.mark.parametrize("change", ("missing", "schema", "parent_status", "parent_launch_sha256",
    "parent_receipt_sha256", "parent_failure_sha256", "verifier_sha256", "promoted", "old_status",
    "same_launch", "same_receipt", "invalid_sha", "extra"))
def test_resealed_retry_lineage_tampering_is_rejected(retry_execution, change):
    fixture = retry_execution
    request = fixture["request"]
    path = fixture["handoff"] / "parent-verification.json"
    proof = worker.read(path)
    recovery = request["recovery_provenance"]
    if change == "missing":
        del request["recovery_provenance"]
    elif change == "promoted":
        proof["native_game_provenance_reverified"] = True
    elif change == "old_status":
        proof["status"] = "passed_active_archive_and_two_stage_checkpoint_scope"
    elif change in ("same_launch", "same_receipt"):
        field = "parent_launch_sha256" if change == "same_launch" else "parent_receipt_sha256"
        proof[field] = recovery[field] = request[field]
    elif change == "invalid_sha":
        proof["parent_failure_sha256"] = recovery["parent_failure_sha256"] = "invalid"
    elif change == "extra":
        recovery["unverified"] = True
    else:
        recovery[change] = "changed"
    path.write_text(json.dumps(proof))
    write_request(fixture)
    with pytest.raises(ValueError, match="recovery provenance"):
        execute(fixture)
    assert not fixture["options"]["output"].exists()
