from dataclasses import dataclass
import math

import torch
import torch.nn.functional as functional

from .fair_final.adapter import source_policy
from .fair_final.learning import conditional_teacher_kl


@dataclass(frozen=True)
class MigrationLossWeights:
    conditional_policy_kl: float
    greedy_nll: float
    wdl_teacher_kl: float

    def __post_init__(self):
        values = (self.conditional_policy_kl, self.greedy_nll, self.wdl_teacher_kl)
        if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in values) or sum(values) == 0):
            raise ValueError("explicit finite nonnegative migration weights required")


def migration_loss(teacher_policies, student_policies, teacher_wdl_logits, student_wdl_logits, *, weights):
    count = len(student_policies)
    if (not isinstance(weights, MigrationLossWeights) or count == 0
            or len(teacher_policies) != count
            or teacher_wdl_logits.shape != (count, 3) or student_wdl_logits.shape != (count, 3)
            or teacher_wdl_logits.dtype != torch.float32 or student_wdl_logits.dtype != torch.float32
            or teacher_wdl_logits.device != student_wdl_logits.device
            or not bool(torch.isfinite(teacher_wdl_logits).all())
            or not bool(torch.isfinite(student_wdl_logits).all())):
        raise ValueError("aligned FP32 migration policy and WDL batch required")
    if any(tuple(policy.temperatures) != (1., 1., 1., 1.) for policy in teacher_policies):
        raise ValueError("migration teacher must use frozen unit temperatures")
    conditional_policy_kl = torch.stack([conditional_teacher_kl(teacher, student)
        for teacher, student in zip(teacher_policies, student_policies)]).mean()
    with torch.no_grad():
        generator = torch.Generator().manual_seed(0)
        actions = [teacher.sample(generator, greedy=True)[0] for teacher in teacher_policies]
        teacher_log = teacher_wdl_logits.detach().log_softmax(-1)
        teacher_probability = teacher_log.exp()
    greedy_nll = -torch.stack([student.log_prob(action)
        for student, action in zip(student_policies, actions)]).mean()
    wdl_teacher_kl = (teacher_probability *
        (teacher_log - functional.log_softmax(student_wdl_logits, dim=-1))).sum(-1).mean()
    loss = (weights.conditional_policy_kl * conditional_policy_kl
        + weights.greedy_nll * greedy_nll + weights.wdl_teacher_kl * wdl_teacher_kl)
    if not all(bool(torch.isfinite(value)) for value in
            (conditional_policy_kl, greedy_nll, wdl_teacher_kl, loss)):
        raise ValueError("nonfinite migration objective")
    return dict(loss=loss, conditional_policy_kl=conditional_policy_kl,
        greedy_nll=greedy_nll, wdl_teacher_kl=wdl_teacher_kl, rows=count)


def migration_batch_loss(teacher, student, frame, legalities, *, weights):
    if any(module.training for module in teacher.modules()) or any(
            parameter.requires_grad for parameter in teacher.parameters()):
        raise ValueError("migration teacher must be frozen and in eval mode")
    if not legalities or frame.batch_size != len(legalities):
        raise ValueError("one migration legality per frame required")
    if any(parameter.dtype != torch.float32 for model in (teacher, student) for parameter in model.parameters()):
        raise ValueError("migration requires FP32 models")
    with torch.no_grad():
        teacher_rows = [source_policy(teacher, frame.narrow_batch(index, 1), legality)
            for index, legality in enumerate(legalities)]
        teacher_policies = [policy for policy, logits in teacher_rows]
        teacher_wdl = torch.cat([logits for policy, logits in teacher_rows], dim=0)
    student_policies, student_wdl = student(frame, legalities)
    return migration_loss(teacher_policies, student_policies, teacher_wdl, student_wdl, weights=weights)
