import copy
import json
import hashlib
from dataclasses import replace

import pytest

import prototype_contract as contract
from test_teacher_targets import verified_record


def complete_result():
    plan = {"learner_deck_identity": {"id": "hog-test"}}
    content = hashlib.sha256(json.dumps(plan["learner_deck_identity"], sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()
    record = replace(verified_record(), content_pack_sha256=content,
                     visit_counts={"wait": 8, "hog": 24}, teacher_policy={"wait": .25, "hog": .75})
    result = {"schema": "hog-event-puct-acceptance.v1", "passed": True, "simulations": 32,
              "episode_id": "game", "seed": 3, "port": 29920, "device": "cuda:0",
              "checkpoint_sha256": "a" * 64, "root_fingerprint": "d" * 64,
              "restored_root_fingerprint": "d" * 64, "snapshot_handles_after": 0,
              "execution": {"submitted": 2, "executed": 2, "confirmed": 2, "rejected": 0,
                            "unconfirmed": 0, "cancelled_native_terminal": 0},
              "teacher_record": record.to_dict(), "episode": record.terminal_evidence,
              "root_tick": 0, "root_actor": 0, "root_legal_action_count": 2, "selected_action": "hog"}
    job = {key: result[key] for key in ("episode_id", "seed", "port", "device")}
    return result, job, {"start_checkpoint_sha256": "a" * 64}, plan


def test_fixed_job_allocation():
    config = {"fleet": contract.FLEET.copy()}
    planned = contract.jobs(config)
    assert len(planned) == 108
    assert planned[0]["episode_id"] == "prototype-0001"
    assert planned[-1]["episode_id"] == "prototype-0108"


def test_job_allocation_rejects_wrong_fleet():
    with pytest.raises(ValueError, match="fixed five-host fleet"):
        contract.jobs({"fleet": {"34": 1}})


def test_complete_episode_is_auditable():
    result, job, config, plan = complete_result()
    contract.validate_result(result, job, config, plan)


def test_evidence_tampering_is_rejected():
    result, job, config, plan = complete_result()
    broken = copy.deepcopy(result)
    broken["snapshot_handles_after"] = 1
    with pytest.raises(ValueError, match="snapshot handles"):
        contract.validate_result(broken, job, config, plan)


@pytest.mark.parametrize("field,value", [("episode_id", "other-game"), ("seed", 999),
                                         ("port", 29921), ("device", "cuda:7"),
                                         ("checkpoint_sha256", "e" * 64), ("simulations", 128),
                                         ("restored_root_fingerprint", "e" * 64),
                                         ("selected_action", "wait")])
def test_wrong_job_or_search_contract_is_rejected(field, value):
    result, job, config, plan = complete_result()
    result[field] = value
    with pytest.raises(ValueError):
        contract.validate_result(result, job, config, plan)


@pytest.mark.parametrize("field,value", [("rejected", 1), ("unconfirmed", 1),
                                         ("confirmed", 0), ("submitted", 10), ("executed", False)])
def test_unclean_execution_evidence_is_rejected(field, value):
    result, job, config, plan = complete_result()
    result["execution"][field] = value
    with pytest.raises(ValueError):
        contract.validate_result(result, job, config, plan)


def test_missing_top_four_reply_and_mismatched_native_probe_are_rejected():
    result, job, config, plan = complete_result()
    with pytest.raises(ValueError, match="probe identity"):
        contract.validate_result(result, job, config, plan, native_runtime_sha256="e" * 64)
    result["teacher_record"]["value_evidence"]["reply_values"].pop("d")
    with pytest.raises(ValueError):
        contract.validate_result(result, job, config, plan)


def test_terminal_result_and_teacher_policy_are_recomputed():
    result, job, config, plan = complete_result()
    result["episode"] = {**result["episode"], "winner": 1}
    with pytest.raises(ValueError):
        contract.validate_result(result, job, config, plan)
    result, job, config, plan = complete_result()
    result["teacher_record"]["teacher_policy"] = {"wait": .5, "hog": .5}
    with pytest.raises(ValueError, match="visit distribution"):
        contract.validate_result(result, job, config, plan)
