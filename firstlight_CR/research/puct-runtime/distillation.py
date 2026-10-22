"""Teacher target records and the additional policy/value distillation loss."""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields
import hashlib
import math
from typing import Mapping, Sequence

import torch
from torch import Tensor
import torch.nn.functional as F

from state_codec import _decode, _encode
from native_runner.training.v4.tensors import UniversalSemanticBatchV4
from tree_search import OpponentResponse, aggregate_opponent_values


_MASK_NAMES = {"gate_mask", "card_mask", "cell_masks", "delay_mask"}
_SCHEMA_V1 = "hog-tree-teacher-record.v1"
_SCHEMA_V2 = "hog-tree-teacher-record.v2"
_SCHEMA_V3 = "hog-tree-teacher-record.v3"


def _check_model_inputs(frame: UniversalSemanticBatchV4,
                        masks: Mapping[str, Tensor]) -> None:
    if not isinstance(frame, UniversalSemanticBatchV4) or frame.batch_size != 1:
        raise ValueError("teacher frame must be one V4 semantic decision")
    if not isinstance(masks, Mapping) or set(masks) != _MASK_NAMES:
        raise ValueError("teacher action masks have the wrong contract")
    if any(not isinstance(mask, Tensor) or mask.dtype != torch.bool for mask in masks.values()):
        raise ValueError("teacher action masks must be boolean")
    cards = frame.own_cards.mask.shape[1]
    if (masks["gate_mask"].shape != (1, 2) or
            masks["card_mask"].shape != (1, cards) or
            masks["cell_masks"].ndim != 3 or masks["cell_masks"].shape[:2] != (1, cards) or
            masks["delay_mask"].ndim != 3 or masks["delay_mask"].shape[:2] != (1, cards) or
            masks["cell_masks"].shape[2] == 0 or masks["delay_mask"].shape[2] == 0 or
            not bool(masks["gate_mask"][0, 0])):
        raise ValueError("teacher action masks do not match the semantic frame")


def encode_model_inputs(frame: UniversalSemanticBatchV4,
                        masks: Mapping[str, Tensor]) -> tuple[dict, dict]:
    """Keep the exact FAIR frame and legality masks used at the teacher root."""
    _check_model_inputs(frame, masks)
    stored = frame.to_storage("cpu", float_dtype=None)
    return _encode(stored), _encode({name: mask.detach().cpu() for name, mask in masks.items()})


@dataclass(frozen=True)
class TeacherRecord:
    episode_id: str
    decision_index: int
    observation: Mapping[str, object]
    legal_actions: tuple[str, ...]
    visit_counts: Mapping[str, int]
    teacher_policy: Mapping[str, float]
    q_expected: float
    q_robust: float
    q_target: float
    terminal_result: float
    opponent_sha256: str
    search_simulations: int
    teacher_temperature: float
    opponent_temperature: float
    state_mode: str
    seed: int
    native_runtime_sha256: str
    content_pack_sha256: str
    semantic_frame: Mapping[str, object] | None = None
    action_masks: Mapping[str, object] | None = None
    value_evidence: Mapping[str, object] | None = None
    terminal_evidence: Mapping[str, object] | None = None

    def model_inputs(self, device: torch.device | str) -> tuple[UniversalSemanticBatchV4, dict[str, Tensor]]:
        if self.semantic_frame is None or self.action_masks is None:
            raise ValueError("teacher record has no captured semantic frame and action masks")
        frame = _decode(self.semantic_frame)
        masks = _decode(self.action_masks)
        _check_model_inputs(frame, masks)
        return frame.to_model_input(device, float_dtype=torch.float32), {
            name: mask.to(device) for name, mask in masks.items()
        }

    def validate(self) -> None:
        if not self.episode_id or self.decision_index < 0:
            raise ValueError("invalid teacher identity")
        if set(self.teacher_policy) != set(self.legal_actions) or set(self.visit_counts) != set(self.legal_actions):
            raise ValueError("teacher policy and visits must cover exactly the legal actions")
        if any(v < 0 or not math.isfinite(v) for v in self.teacher_policy.values()):
            raise ValueError("teacher probabilities must be finite and non-negative")
        if not math.isclose(sum(self.teacher_policy.values()), 1.0, abs_tol=1e-5):
            raise ValueError("teacher policy must sum to one")
        if any(not isinstance(v, int) or v < 0 for v in self.visit_counts.values()):
            raise ValueError("visit counts must be non-negative integers")
        if sum(self.visit_counts.values()) != self.search_simulations:
            raise ValueError("search visit count does not match simulation budget")
        for value in (self.q_expected, self.q_robust, self.q_target, self.terminal_result):
            if not math.isfinite(value):
                raise ValueError("teacher values must be finite")
        if self.search_simulations not in (32, 128, 256) or self.state_mode != "deterministic_full_state":
            raise ValueError("teacher search contract mismatch")
        if not math.isfinite(self.teacher_temperature) or self.teacher_temperature <= 0:
            raise ValueError("teacher temperature must be finite and positive")
        if self.opponent_temperature != .5:
            raise ValueError("frozen opponent temperature mismatch")
        if not math.isclose(self.q_target, .7 * self.q_expected + .3 * self.q_robust, abs_tol=1e-6):
            raise ValueError("Q target cannot be recomputed")
        for digest in (self.opponent_sha256, self.native_runtime_sha256, self.content_pack_sha256):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("invalid teacher identity SHA")
        if (self.semantic_frame is None) != (self.action_masks is None):
            raise ValueError("teacher frame and action masks must be captured together")
        if self.semantic_frame is not None:
            self.model_inputs("cpu")
        if (self.value_evidence is None) != (self.terminal_evidence is None):
            raise ValueError("teacher value and terminal evidence must be captured together")
        if self.value_evidence is not None:
            self._validate_training_evidence()

    def _validate_training_evidence(self) -> None:
        evidence, terminal = self.value_evidence, self.terminal_evidence
        if self.semantic_frame is None or evidence is None or terminal is None:
            raise ValueError("verified model inputs, Top-K replies and terminal evidence are required")
        if (evidence.get("version") != "frozen-opponent-top-k.v1" or
                evidence.get("root_action") not in self.legal_actions or
                evidence.get("root_owner") not in (0, 1) or
                evidence.get("opponent_owner") != 1 - evidence["root_owner"] or
                evidence.get("top_k") != 4 or evidence.get("temperature") != self.opponent_temperature):
            raise ValueError("teacher frozen Top-K evidence contract mismatch")
        probabilities, values = evidence.get("reply_probabilities"), evidence.get("reply_values")
        count = evidence.get("reply_legal_action_count")
        if (type(count) is not int or count < 1 or not isinstance(probabilities, Mapping) or
                not isinstance(values, Mapping) or set(probabilities) != set(values) or
                len(values) != min(4, count) or
                not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-6)):
            raise ValueError("all and only frozen Top-K replies need evaluated values")
        expected, robust = aggregate_opponent_values([
            OpponentResponse(key, probability, values[key]) for key, probability in probabilities.items()
        ])
        if not (math.isclose(expected, self.q_expected, abs_tol=1e-6) and
                math.isclose(robust, self.q_robust, abs_tol=1e-6)):
            raise ValueError("teacher values differ from evaluated frozen replies")
        for digest in (evidence.get("reply_state_sha256", ""),
                       evidence.get("frozen_prior_sha256", ""), terminal.get("native_state_sha256", "")):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid teacher evidence SHA")
        if (terminal.get("version") != "native-terminal-evidence.v1" or
                terminal.get("episode_id") != self.episode_id or terminal.get("ended") is not True or
                terminal.get("truncated") is not False or terminal.get("winner") not in (None, 0, 1) or
                type(terminal.get("terminal_tick")) is not int or
                terminal["terminal_tick"] < self.observation.get("tick", 0) or
                not terminal.get("reason")):
            raise ValueError("teacher terminal evidence is incomplete or truncated")
        winner = terminal["winner"]
        results = [0.0, 0.0] if winner is None else ([1.0, -1.0] if winner == 0 else [-1.0, 1.0])
        if (list(terminal.get("result_by_owner", ())) != results or
                self.terminal_result != results[evidence["root_owner"]]):
            raise ValueError("teacher terminal result differs from native settlement")

    def require_training_ready(self) -> None:
        """Exclude legacy/prototype placeholders from every training consumer."""
        self.validate()
        self._validate_training_evidence()

    def to_dict(self) -> dict:
        self.validate()
        data = asdict(self)
        if self.value_evidence is not None:
            return {"schema": _SCHEMA_V3, **data}
        del data["value_evidence"]
        del data["terminal_evidence"]
        if self.semantic_frame is None:
            del data["semantic_frame"]
            del data["action_masks"]
            return {"schema": _SCHEMA_V1, **data}
        return {"schema": _SCHEMA_V2, **data}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TeacherRecord:
        names = {item.name for item in fields(cls)}
        schema = value.get("schema")
        required = names if schema == _SCHEMA_V3 else names - {"value_evidence", "terminal_evidence"}
        if schema == _SCHEMA_V1:
            required -= {"semantic_frame", "action_masks"}
        if schema not in {_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3} or set(value) - {"schema"} != required:
            raise ValueError("teacher record schema mismatch")
        data = {name: value[name] for name in required}
        data["legal_actions"] = tuple(data["legal_actions"])
        record = cls(**data)
        if schema in {_SCHEMA_V2, _SCHEMA_V3} and record.semantic_frame is None:
            raise ValueError("teacher record requires model inputs")
        record.validate()
        if schema == _SCHEMA_V3:
            record.require_training_ready()
        return record


def split_episodes(records: Sequence[TeacherRecord], *, seed: int = 1755) -> dict[str, list[TeacherRecord]]:
    """Split whole episodes, never individual decisions, into 80/10/10 sets."""
    groups: dict[str, list[TeacherRecord]] = {"train": [], "validation": [], "test": []}
    membership = {}
    for episode in {record.episode_id for record in records}:
        digest = hashlib.sha256(f"{seed}:{episode}".encode()).digest()
        fraction = int.from_bytes(digest[:8], "big") / 2**64
        membership[episode] = "train" if fraction < .8 else "validation" if fraction < .9 else "test"
    identities: set[tuple[str, int]] = set()
    for record in records:
        record.validate()
        identity = (record.episode_id, record.decision_index)
        if identity in identities:
            raise ValueError("duplicate teacher decision")
        identities.add(identity)
        groups[membership[record.episode_id]].append(record)
    return groups


def scheduled_weights(update: int, *, total_updates: int = 200, start: int = 100,
                      policy_start: float = 0.50, value_start: float = 0.10,
                      policy_end: float = 0.25, value_end: float = 0.05) -> tuple[float, float]:
    if not 0 <= update <= total_updates or not 0 <= start <= total_updates:
        raise ValueError("update schedule out of range")
    ratio = 0.0 if update <= start else min(1.0, (update - start) / max(1, total_updates - start))
    return (policy_start + ratio * (policy_end - policy_start),
            value_start + ratio * (value_end - value_start))


def distillation_loss(policy_log_probs: Tensor, teacher_probs: Tensor, value: Tensor,
                      q_target: Tensor, *, policy_weight: float, value_weight: float,
                      legal_mask: Tensor | None = None) -> tuple[Tensor, Tensor, Tensor, Tensor]:
    if policy_log_probs.shape != teacher_probs.shape:
        raise ValueError("policy and teacher distributions must share shape")
    if value.shape != q_target.shape:
        raise ValueError("value and q_target must share shape")
    if policy_weight < 0 or value_weight < 0:
        raise ValueError("distillation weights must be non-negative")
    mask = torch.ones_like(teacher_probs, dtype=torch.bool) if legal_mask is None else legal_mask.bool()
    if mask.shape != teacher_probs.shape or not bool(mask.any(-1).all()):
        raise ValueError("every teacher row needs a legal action")
    if not bool(torch.isfinite(policy_log_probs[mask]).all() and torch.isfinite(teacher_probs).all()
                and torch.isfinite(value).all() and torch.isfinite(q_target).all()):
        raise ValueError("distillation inputs must be finite")
    if bool((teacher_probs < 0).any() or (teacher_probs.sum(-1) <= 0).any() or
            (teacher_probs[~mask] != 0).any()):
        raise ValueError("teacher distribution must have positive non-negative mass")
    teacher = teacher_probs / teacher_probs.sum(-1, keepdim=True)
    policy_ce = -(teacher * torch.where(mask, policy_log_probs, 0.0)).sum(-1).mean()
    value_huber = F.huber_loss(value, q_target, reduction="mean")
    total = policy_weight * policy_ce + value_weight * value_huber
    return total, policy_ce, value_huber, q_target.detach()
