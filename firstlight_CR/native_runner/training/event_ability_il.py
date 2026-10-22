"""Skill-only IL over frozen event-policy features and native-verified labels.

The binary gate marginalizes WAIT/PLAY, so no old action is an IL target.
An independent optimizer is restricted to the four new parameter tensors.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import math

import torch
from torch import Tensor
import torch.nn.functional as F

ABILITY_PARAMETER_NAMES = frozenset({
    "ability_gate.weight", "ability_gate.bias", "ability_query.weight", "ability_query.bias",
})


def tensor_digest(value: Tensor) -> str:
    value = value.detach().cpu().contiguous()
    digest = hashlib.sha256(str((value.dtype, tuple(value.shape))).encode())
    digest.update(value.reshape(-1).view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def training_state_digest(value) -> str:
    """Stable digest for optimizer/controller payloads, independent of pickle."""
    digest = hashlib.sha256()
    def visit(item):
        if isinstance(item, Tensor):
            digest.update(b'tensor:' + tensor_digest(item).encode())
        elif isinstance(item, dict):
            digest.update(b'dict:')
            for key in sorted(item, key=lambda key: (type(key).__name__, repr(key))):
                visit(key)
                visit(item[key])
        elif isinstance(item, (tuple, list)):
            digest.update(type(item).__name__.encode() + str(len(item)).encode() + b':')
            for child in item:
                visit(child)
        elif item is None or isinstance(item, (str, int, float, bool)):
            digest.update((type(item).__name__ + ':' + repr(item) + '\n').encode())
        else:
            raise TypeError(f'unsupported training state: {type(item)}')
    visit(value)
    return digest.hexdigest()


def frozen_state_digests(model) -> dict[str, str]:
    return {name: tensor_digest(value) for name, value in model.state_dict().items()
            if name not in ABILITY_PARAMETER_NAMES}


def freeze_for_ability_il(model) -> tuple[Tensor, ...]:
    if not model.config.ability_actions:
        raise ValueError("skill-only IL requires an explicitly migrated ability checkpoint")
    parameters = dict(model.named_parameters())
    if not ABILITY_PARAMETER_NAMES <= parameters.keys():
        raise ValueError("missing skill parameters")
    model.eval()
    for name, parameter in parameters.items():
        parameter.requires_grad_(name in ABILITY_PARAMETER_NAMES)
        parameter.grad = None
    return tuple(parameter for name, parameter in parameters.items() if name in ABILITY_PARAMETER_NAMES)


def verify_frozen_state(model, expected: dict[str, str]) -> None:
    actual = frozen_state_digests(model)
    changed = sorted(name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name))
    if changed:
        raise ValueError(f"frozen model state changed: {changed}")


@dataclass(frozen=True)
class AbilityILFeatures:
    policy_context: Tensor
    candidate_memory: Tensor
    ability_mask: Tensor
    old_gate_logits: Tensor
    old_gate_mask: Tensor
    # -1 means no immediate skill; otherwise an exact native source candidate.
    target: Tensor

    def to(self, device) -> "AbilityILFeatures":
        return AbilityILFeatures(**{f.name: getattr(self, f.name).to(device) for f in fields(self)})

    def select(self, indices) -> "AbilityILFeatures":
        return AbilityILFeatures(**{f.name: getattr(self, f.name)[indices] for f in fields(self)})

    def validate(self) -> None:
        n = self.target.numel()
        if n == 0 or self.target.shape != (n,) or self.target.dtype != torch.int64:
            raise ValueError("skill IL needs nonempty int64 targets")
        if self.policy_context.ndim != 2 or self.policy_context.shape[0] != n:
            raise ValueError("invalid skill policy features")
        if self.candidate_memory.ndim != 3 or self.candidate_memory.shape[0] != n:
            raise ValueError("invalid source features")
        if self.ability_mask.shape != self.candidate_memory.shape[:2] or self.ability_mask.dtype != torch.bool:
            raise ValueError("invalid native skill mask")
        if self.old_gate_logits.shape != (n, 2) or self.old_gate_mask.shape != (n, 2) or self.old_gate_mask.dtype != torch.bool:
            raise ValueError("invalid frozen WAIT/PLAY gate")
        if not self.ability_mask.any(-1).all() or not self.old_gate_mask.any(-1).all():
            raise ValueError("skill IL requires a legal skill and a legal non-skill alternative")
        for value in (self.policy_context, self.candidate_memory, self.old_gate_logits):
            if value.requires_grad or not torch.isfinite(value).all():
                raise ValueError("features must be finite and detached from the frozen encoder")
        positive = self.target >= 0
        if (self.target < -1).any() or (self.target >= self.ability_mask.shape[1]).any():
            raise ValueError("source target is outside the snapshot")
        if not self.ability_mask[positive, self.target[positive]].all():
            raise ValueError("source target is not native-legal")


@torch.no_grad()
def ability_il_features(model, context, *, old_gate_mask: Tensor, target: Tensor) -> AbilityILFeatures:
    if model.training:
        raise ValueError("frozen encoder must remain in eval mode")
    result = AbilityILFeatures(
        context.policy_context.detach().clone(), context.encoded.candidate_memory.detach().clone(),
        context.ability_mask.detach().clone(), model.gate_head(context.policy_context).detach().clone(),
        old_gate_mask.detach().clone(), target.detach().clone())
    result.validate()
    return result


def ability_il_loss(model, features: AbilityILFeatures) -> tuple[Tensor, dict[str, Tensor]]:
    features.validate()
    # This is exactly the skill marginal of the three-way inference gate at T=1.
    non_skill = features.old_gate_logits.masked_fill(~features.old_gate_mask, -torch.inf).logsumexp(-1)
    skill = model.ability_gate(features.policy_context).squeeze(-1)
    positive = features.target >= 0
    binary_logit = skill - non_skill
    gate_loss = F.binary_cross_entropy_with_logits(binary_logit, positive.to(skill.dtype))
    source_logits = torch.einsum("bd,bcd->bc", model.ability_query(features.policy_context),
                                 features.candidate_memory) / math.sqrt(model.card_dim)
    source_logits = source_logits.masked_fill(~features.ability_mask, -torch.inf)
    source_loss = (F.cross_entropy(source_logits[positive], features.target[positive], reduction="sum")
                   / positive.numel()) if positive.any() else skill.sum() * 0
    with torch.no_grad():
        predicted = binary_logit >= 0
        metrics = dict(gate_loss=gate_loss.detach(), source_loss=source_loss.detach(),
            rows=positive.new_tensor(positive.numel(), dtype=torch.int64), positives=positive.sum(),
            true_positive=(predicted & positive).sum(), false_positive=(predicted & ~positive).sum(),
            false_negative=(~predicted & positive).sum(),
            source_correct=(source_logits[positive].argmax(-1) == features.target[positive]).sum(),
            probability_sum=binary_logit.sigmoid().sum())
    return gate_loss + source_loss, metrics


def make_ability_optimizer(model, *, learning_rate: float = 1e-3) -> torch.optim.AdamW:
    if not math.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError("skill learning rate must be positive and finite")
    return torch.optim.AdamW(freeze_for_ability_il(model), lr=learning_rate, weight_decay=0.)


def ability_il_step(model, optimizer, features: AbilityILFeatures, *, max_grad_norm: float = 1.) -> dict[str, float]:
    named = dict(model.named_parameters())
    trainable = {name for name, parameter in named.items() if parameter.requires_grad}
    optimized = [parameter for group in optimizer.param_groups for parameter in group["params"]]
    if (model.training or trainable != ABILITY_PARAMETER_NAMES or len(optimized) != len(trainable)
            or {id(parameter) for parameter in optimized} != {id(named[name]) for name in trainable}):
        raise ValueError("skill IL may optimize only the four new parameters with the model in eval mode")
    optimizer.zero_grad(set_to_none=True)
    loss, metrics = ability_il_loss(model, features)
    if not torch.isfinite(loss):
        raise ValueError("nonfinite skill IL loss")
    loss.backward()
    norm = torch.nn.utils.clip_grad_norm_(optimized, max_grad_norm, error_if_nonfinite=True)
    optimizer.step()
    return dict(loss=float(loss.detach()), grad_norm=float(norm),
                **{name: float(value) for name, value in metrics.items()})
