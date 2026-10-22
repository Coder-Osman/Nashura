"""Audited feature shards for frozen skill-head imitation learning."""
from __future__ import annotations

from dataclasses import fields
import hashlib
from pathlib import Path

import torch
import torch.nn.functional as F

from .event_ability_il import AbilityILFeatures

SCHEMA = "event-ability-head-il-features.v1"


def replay_partition(replay_tag: str) -> str:
    # A whole replay (both owners, all frames) belongs to one partition.
    return "validation" if int(hashlib.sha256(replay_tag.encode()).hexdigest()[:8], 16) % 5 == 0 else "train"


def concatenate_features(batches: list[AbilityILFeatures]) -> AbilityILFeatures:
    if not batches:
        raise ValueError("no verified ability features")
    width = max(batch.ability_mask.shape[1] for batch in batches)
    values = {}
    for field in fields(AbilityILFeatures):
        tensors = []
        for batch in batches:
            value = getattr(batch, field.name)
            if field.name == "candidate_memory":
                value = F.pad(value, (0, 0, 0, width - value.shape[1]))
            elif field.name == "ability_mask":
                value = F.pad(value, (0, width - value.shape[1]), value=False)
            tensors.append(value)
        values[field.name] = torch.cat(tensors)
    result = AbilityILFeatures(**values)
    result.validate()
    return result


def validate_label(row: dict, target: int) -> None:
    required = ("replay_tag", "payload_sha256", "owner", "tick", "legal_sources", "partition")
    if any(key not in row for key in required) or row["owner"] not in (0, 1):
        raise ValueError("skill label lacks native replay provenance")
    if row["partition"] != replay_partition(row["replay_tag"]):
        raise ValueError("replay partition mismatch")
    if row.get("trusted") is not True:
        raise ValueError("untrusted replay row")
    sources = row["legal_sources"]
    if not sources or len({x["candidate"] for x in sources}) != len(sources):
        raise ValueError("empty or duplicate legal source audit")
    if target == -1:
        if row.get("expert_skill_next_tick") is not False:
            raise ValueError("negative label overlaps an expert skill")
        return
    matches = [source for source in sources if source["candidate"] == target]
    if len(matches) != 1 or row.get("expert_skill_next_tick") is not True:
        raise ValueError("skill source label disagrees with native snapshot")
    source, receipt = matches[0], row.get("receipt", {})
    if row.get("source_resolution") == "unique-native-ready":
        if len(sources) != 1:
            raise ValueError("ambiguous source must not receive a guessed label")
    else:
        raise ValueError("unsupported skill source evidence")
    if (receipt.get("event_type") != "action_executed" or receipt.get("owner") != row["owner"]
            or receipt.get("entity_id") != source["source_entity"]
            or receipt.get("tick") != row["tick"] + 1
            or receipt.get("data", {}).get("ability_id") != source["ability_id"]
            or receipt.get("data", {}).get("kind") != "activate_ability"
            or receipt.get("data", {}).get("fair_ability_activation_exact") is not True):
        raise ValueError("skill label lacks an exact next-tick native execution receipt")


def write_features(path: Path, batches: list[AbilityILFeatures], rows: list[dict], *, parent_sha256: str) -> str:
    features = concatenate_features(batches).to("cpu")
    if len(rows) != features.target.numel():
        raise ValueError("feature and audit row counts disagree")
    for row, target in zip(rows, features.target.tolist(), strict=True):
        validate_label(row, target)
    payload = dict(schema=SCHEMA, parent_sha256=parent_sha256, rows=rows,
        features={f.name: getattr(features, f.name) for f in fields(features)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        torch.save(payload, stream)
    loaded, audited = read_features(path, parent_sha256=parent_sha256)
    if audited != rows or any(not torch.equal(getattr(loaded, f.name), getattr(features, f.name)) for f in fields(features)):
        raise ValueError("skill feature readback mismatch")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_features(path: Path, *, parent_sha256: str) -> tuple[AbilityILFeatures, list[dict]]:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    if payload.get("schema") != SCHEMA or payload.get("parent_sha256") != parent_sha256:
        raise ValueError("skill features belong to a different schema or parent weight")
    features = AbilityILFeatures(**payload["features"])
    features.validate()
    rows = payload["rows"]
    if len(rows) != features.target.numel():
        raise ValueError("feature and audit row counts disagree")
    for index, (row, target) in enumerate(zip(rows, features.target.tolist(), strict=True)):
        validate_label(row, target)
        audited = {source["candidate"] for source in row["legal_sources"]}
        actual = set(features.ability_mask[index].nonzero().flatten().tolist())
        if audited != actual:
            raise ValueError("native audit and feature source masks disagree")
    return features, rows
