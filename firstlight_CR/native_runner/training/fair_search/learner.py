import hashlib
import math
import time

import torch
import torch.nn.functional as functional

from ..event_model import EventActionV1
from ..event_ppo import _stack_frames
from ..v4.tensors import concatenate_tensor_records
from .config import SearchConfig


def decode_action(key, device):
    if key == "wait":
        return EventActionV1(torch.tensor([False], device=device),
                             *[torch.tensor([-1], device=device) for _ in range(3)])
    prefix, row, cell, delay = key.split(":")
    if prefix != "play":
        raise ValueError("Hog search only accepts card actions")
    return EventActionV1(torch.tensor([True], device=device),
                         *[torch.tensor([int(value)], device=device) for value in (row, cell, delay)])


def decision_loss(model, decision, *, teacher=None, teacher_sha256=None,
                  prior_fraction=0., prior_samples=8, detach_value_features=False):
    if not 0 <= prior_fraction <= 1:
        raise ValueError("prior fraction must be in [0, 1]")
    if prior_fraction and (teacher is None or teacher_sha256 != decision.model_sha256
                           or type(prior_samples) is not int or prior_samples < 1):
        raise ValueError("prior distillation requires the frozen producing model")
    device = next(model.parameters()).device
    context, logits = model.encode(decision.information.frame)
    masks = {name: value.to(device) for name, value in decision.information.masks.items()}
    logs, weights = [], []
    for action, count in decision.visits.items():
        if count:
            if not decision.information.legal(action):
                raise ValueError("positive search mass on illegal action")
            step = model.policy.evaluate_actions(context, decode_action(action, device),
                                                 temperatures=(1., 1., 1., 1.), **masks, validate=True)
            logs.append(step.log_prob[0])
            weights.append(count / 32.)
    visit_loss = -(torch.stack(logs) * torch.tensor(weights, device=device)).sum()
    policy_loss = visit_loss
    if prior_fraction:
        teacher_device = next(teacher.parameters()).device
        seed = int.from_bytes(hashlib.sha256(
            f"{teacher_sha256}:{decision.information.sha256}:{prior_samples}".encode()).digest()[:8], "big")
        with torch.no_grad(), torch.random.fork_rng(
                devices=[teacher_device.index or 0] if teacher_device.type == "cuda" else []):
            torch.manual_seed(seed)
            teacher_context, _ = teacher.encode(decision.information.frame)
            indices = torch.zeros(prior_samples, dtype=torch.long, device=teacher_device)
            teacher_masks = {name: value.to(teacher_device).expand(prior_samples, *value.shape[1:])
                             for name, value in decision.information.masks.items()}
            samples = teacher.policy.select_action(teacher_context.index_select(indices),
                                                   **teacher_masks, sample=True, validate=True).action
        actions = concatenate_tensor_records(tuple(
            EventActionV1(samples.act[index:index + 1].to(device),
                          samples.card_row[index:index + 1].to(device),
                          samples.cell[index:index + 1].to(device),
                          samples.delay_bin[index:index + 1].to(device))
            for index in range(prior_samples)))
        student_context = context.index_select(torch.zeros(prior_samples, dtype=torch.long, device=device))
        student_masks = {name: value.expand(prior_samples, *value.shape[1:]) for name, value in masks.items()}
        prior_loss = -model.policy.evaluate_actions(student_context, actions,
                                                    **student_masks, validate=True).log_prob.mean()
        policy_loss = (1 - prior_fraction) * visit_loss + prior_fraction * prior_loss
    label = {1: 0, 0: 1, -1: 2}[decision.result]
    if detach_value_features:
        logits = model.wdl_head(context.value_context.detach())
    value_loss = functional.cross_entropy(logits, torch.tensor([label], device=device))
    return policy_loss + value_loss, policy_loss, value_loss


def visit_target(visits, temperature=1.):
    if not math.isfinite(temperature) or temperature < 0:
        raise ValueError("target temperature must be finite and nonnegative")
    if not visits or any(type(count) is not int or count < 0 for count in visits.values()) or sum(visits.values()) != 32:
        raise ValueError("target requires exactly 32 valid visits")
    if temperature == 0:
        action = min(visits, key=lambda key: (-visits[key], key))
        return {action: 1.}
    if temperature == 1:
        return {action: count / 32. for action, count in visits.items() if count}
    maximum = max(visits.values())
    weights = {action: math.exp(math.log(count / maximum) / temperature)
               for action, count in visits.items() if count}
    total = sum(weights.values())
    return {action: weight / total for action, weight in weights.items() if weight}


def decision_batch_loss(model, decisions, *, detach_value_features=False, target_temperature=1.):
    if not decisions:
        raise ValueError("decision batch must not be empty")
    device = next(model.parameters()).device
    context, logits = model.encode(_stack_frames(tuple(row.information.frame for row in decisions)))
    masks = {name: torch.cat(tuple(row.information.masks[name] for row in decisions)).to(device)
             for name in decisions[0].information.masks}
    indices, weights, components = [], [], []
    for index, decision in enumerate(decisions):
        target = visit_target(decision.visits, target_temperature)
        for action, count in decision.visits.items():
            if count and not decision.information.legal(action):
                raise ValueError("positive search mass on illegal action")
        for action, weight in target.items():
            if not decision.information.legal(action):
                raise ValueError("positive search mass on illegal action")
            indices.append(index)
            weights.append(weight)
            components.append((False, -1, -1, -1) if action == "wait" else
                              (True, *map(int, action.split(":")[1:])))
    action = EventActionV1(*(torch.tensor(values, device=device, dtype=torch.bool if column == 0 else torch.long)
                            for column, values in enumerate(zip(*components))))
    indices = torch.tensor(indices, device=device)
    step = model.policy.evaluate_actions(
        context.index_select(indices), action,
        **{name: mask.index_select(0, indices) for name, mask in masks.items()}, validate=True)
    policy_loss = -(step.log_prob * torch.tensor(weights, device=device)).sum() / len(decisions)
    if detach_value_features:
        logits = model.wdl_head(context.value_context.detach())
    labels = torch.tensor([{1: 0, 0: 1, -1: 2}[row.result] for row in decisions], device=device)
    value_loss = functional.cross_entropy(logits, labels)
    return policy_loss + value_loss, policy_loss, value_loss


class SearchLearner:
    def __init__(self, model, replay, *, config=None, teacher=None, teacher_sha256=None,
                 prior_fraction=0., prior_samples=8, detach_value_features=False, microbatch_size=32,
                 target_temperature=1.):
        self.config = config or SearchConfig()
        self.model, self.replay = model, replay
        if prior_fraction and (teacher is None or teacher_sha256 is None):
            raise ValueError("prior distillation requires a producing checkpoint")
        self.teacher = teacher.eval().requires_grad_(False) if teacher is not None else None
        self.teacher_sha256 = teacher_sha256
        self.prior_fraction = prior_fraction
        self.prior_samples = prior_samples
        self.detach_value_features = detach_value_features
        visit_target({"wait": 32}, target_temperature)
        if prior_fraction and target_temperature != 1:
            raise ValueError("target temperature experiment excludes prior distillation")
        self.target_temperature = target_temperature
        if type(microbatch_size) is not int or microbatch_size < 1:
            raise ValueError("microbatch size must be positive")
        self.microbatch_size = microbatch_size
        self.optimizer = torch.optim.AdamW(model.parameters(), lr=self.config.learning_rate,
                                          weight_decay=self.config.weight_decay)
        self.sample_credit = 0
        self.optimizer_steps = 0

    def ingest(self, episode):
        rows = self.replay.add(episode)
        self.sample_credit += rows * self.config.samples_per_row
        return rows

    def train_available(self, *, max_steps=None):
        if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
            raise ValueError("training step limit must be a positive integer")
        metrics = []
        self.model.train()
        while (self.sample_credit >= self.config.batch_size
               and (max_steps is None or len(metrics) < max_steps)):
            started = time.perf_counter()
            self.optimizer.zero_grad(set_to_none=True)
            policy_sum = value_sum = 0.
            rows = self.replay.sample(self.config.batch_size)
            chunk_size = 1 if self.prior_fraction else self.microbatch_size
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                if self.prior_fraction:
                    loss, policy, value = decision_loss(
                        self.model, chunk[0], teacher=self.teacher, teacher_sha256=self.teacher_sha256,
                        prior_fraction=self.prior_fraction, prior_samples=self.prior_samples,
                        detach_value_features=self.detach_value_features)
                else:
                    loss, policy, value = decision_batch_loss(
                        self.model, chunk, detach_value_features=self.detach_value_features,
                        target_temperature=self.target_temperature)
                if not bool(torch.isfinite(loss)):
                    self.optimizer.zero_grad(set_to_none=True)
                    raise ValueError("nonfinite search objective")
                (loss * (len(chunk) / self.config.batch_size)).backward()
                policy_sum = policy_sum + policy.detach() * len(chunk)
                value_sum = value_sum + value.detach() * len(chunk)
            norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.gradient_clip,
                                                  error_if_nonfinite=True)
            self.optimizer.step()
            self.optimizer_steps += 1
            self.sample_credit -= self.config.batch_size
            metrics.append(dict(step=self.optimizer_steps, policy_ce=float(policy_sum) / self.config.batch_size,
                                value_ce=float(value_sum) / self.config.batch_size, gradient_norm=float(norm),
                                seconds=time.perf_counter() - started, microbatch_size=chunk_size))
        self.model.eval()
        return metrics

    def resume(self, payload):
        if self.target_temperature != 1:
            raise ValueError("target temperature requires an independent experiment checkpoint")
        if payload["config_sha256"] != self.config.sha256:
            raise ValueError("learner configuration differs on resume")
        self.optimizer.load_state_dict(payload["optimizer"])
        self.replay.load_state_dict(payload["replay_state"])
        self.sample_credit = payload["sample_credit"]
        self.optimizer_steps = payload["optimizer_steps"]
