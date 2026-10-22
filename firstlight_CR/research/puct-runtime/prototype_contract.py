"""Fixed prototype allocation and complete per-episode evidence validation."""
from __future__ import annotations

import hashlib
import json
import math

HOST_ORDER = ("37", "34", "35", "39", "41")
FLEET = {"34": 24, "35": 24, "37": 24, "39": 18, "41": 18}
CONCURRENCY = {"34": 2, "35": 8, "37": 8, "39": 6, "41": 6}


def jobs(config: dict) -> list[dict]:
    if config.get("fleet") != FLEET:
        raise ValueError("prototype allocation must match the fixed five-host fleet")
    result = []
    for host in HOST_ORDER:
        for offset in range(FLEET[host]):
            episode = len(result) + 1
            result.append({"host": host, "port": 29920 + offset,
                           "device": f"cuda:{offset % 8}", "episode": episode,
                           "episode_id": f"prototype-{episode:04d}", "seed": 1755 + episode,
                           "output": f"formal/prototype/episode-{episode:04d}.json",
                           "log": f"formal/prototype/episode-{episode:04d}.log",
                           "pid": f"formal/prototype/episode-{episode:04d}.pid"})
    return result


def validate_result(result, job, config, plan, *, native_runtime_sha256=None) -> None:
    from distillation import TeacherRecord

    if not isinstance(result, dict) or result.get("schema") != "hog-event-puct-acceptance.v1":
        raise ValueError("invalid prototype result schema")
    if result.get("passed") is not True or result.get("simulations") != 32:
        raise ValueError("prototype requires a passed 32-simulation result")
    for field in ("episode_id", "seed", "port", "device"):
        if result.get(field) != job[field]:
            raise ValueError(f"prototype job identity mismatch: {field}")
    checkpoint = config["start_checkpoint_sha256"]
    if result.get("checkpoint_sha256") != checkpoint:
        raise ValueError("prototype parent checkpoint is not the fixed 1755 weight")
    fingerprint = result.get("root_fingerprint", "")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
            any(character not in "0123456789abcdef" for character in fingerprint) or
            result.get("restored_root_fingerprint") != fingerprint):
        raise ValueError("prototype root state was not restored exactly")
    if result.get("snapshot_handles_after") != 0:
        raise ValueError("prototype leaked native snapshot handles")
    execution = result.get("execution", {})
    fields = ("submitted", "executed", "confirmed", "rejected", "unconfirmed", "cancelled_native_terminal")
    if any(type(execution.get(field)) is not int or execution[field] < 0 for field in fields):
        raise ValueError("prototype execution ledger is incomplete")
    if (execution["rejected"] or execution["unconfirmed"] or
            execution["executed"] != execution["confirmed"] or
            execution["submitted"] != execution["confirmed"] + execution["cancelled_native_terminal"]):
        raise ValueError("prototype execution ledger is not clean")
    record = TeacherRecord.from_dict(result["teacher_record"])
    record.require_training_ready()
    if (record.episode_id != job["episode_id"] or record.seed != job["seed"] or
            record.decision_index != 0 or record.search_simulations != 32 or
            record.opponent_sha256 != checkpoint or record.teacher_temperature != 1.0):
        raise ValueError("teacher record differs from prototype job contract")
    if native_runtime_sha256 is not None and record.native_runtime_sha256 != native_runtime_sha256:
        raise ValueError("prototype native probe identity mismatch")
    content_sha256 = hashlib.sha256(json.dumps(plan["learner_deck_identity"], sort_keys=True,
                                              separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if record.content_pack_sha256 != content_sha256:
        raise ValueError("prototype content identity mismatch")
    if (result.get("episode") != record.terminal_evidence or
            result.get("root_tick") != record.observation.get("tick") or
            result.get("root_actor") != record.value_evidence["root_owner"] or
            result.get("root_legal_action_count") != len(record.legal_actions)):
        raise ValueError("prototype result differs from captured teacher evidence")
    selected = max(record.legal_actions, key=lambda action: (record.visit_counts[action], action))
    if result.get("selected_action") != selected or record.value_evidence["root_action"] != selected:
        raise ValueError("prototype selected action differs from PUCT visit counts")
    if any(not math.isclose(record.teacher_policy[action], record.visit_counts[action] / 32,
                            abs_tol=1e-6) for action in record.legal_actions):
        raise ValueError("prototype policy differs from root visit distribution")
