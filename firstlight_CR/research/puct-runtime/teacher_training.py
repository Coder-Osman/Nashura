"""Sparse PUCT distillation accumulated into the existing PPO optimizer step."""
from __future__ import annotations

import json
from pathlib import Path
import random

import torch

from distillation import TeacherRecord, distillation_loss
from action_codec import decode_action


def load_teachers(paths):
    records, identities = [], set()
    for source in paths:
        path = Path(source)
        if path.suffix == ".jsonl":
            values = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        else:
            value = json.loads(path.read_text())
            values = value if isinstance(value, list) else [value.get("teacher_record", value)]
        for value in values:
            record = TeacherRecord.from_dict(value)
            record.require_training_ready()
            identity = (record.episode_id, record.decision_index)
            if identity in identities:
                raise ValueError("duplicate teacher decision in training input")
            identities.add(identity)
            records.append(record)
    if not records:
        raise ValueError("training needs verified, completed teacher records")
    return records


def teacher_record_loss(model, record, *, policy_weight, value_weight):
    device = next(model.parameters()).device
    frame, masks = record.model_inputs(device)
    if (masks["card_mask"].shape != (1, model.card_count) or
            masks["cell_masks"].shape != (1, model.card_count, model.cell_count) or
            masks["delay_mask"].shape != (1, model.card_count, model.event_config.delay_bin_count)):
        raise ValueError("teacher masks differ from the learner model contract")
    context = model.encode(frame, model.initial_state(1, device=device),
                           episode_start=torch.ones(1, dtype=torch.bool, device=device))
    logs, probabilities = [], []
    for key, probability in record.teacher_policy.items():
        if probability == 0:
            continue
        action = decode_action(key, device=device)
        if key.startswith("play:"):
            row, cell, delay = int(action.card_row[0]), int(action.cell[0]), int(action.delay_bin[0])
            if (not 0 <= row < model.card_count or not 0 <= cell < model.cell_count or
                    not 0 <= delay < model.event_config.delay_bin_count or
                    not bool(masks["gate_mask"][0, 1] and masks["card_mask"][0, row] and
                             masks["cell_masks"][0, row, cell] and masks["delay_mask"][0, row, delay])):
                raise ValueError("positive teacher mass on a masked play action")
        elif key.startswith("ability:"):
            source, delay = int(action.ability_candidate[0]), int(action.delay_bin[0])
            if (not model.config.ability_actions or source >= context.ability_mask.shape[1] or
                    not bool(context.ability_mask[0, source]) or
                    (model.config.ability_delay and not 0 <= delay < model.event_config.delay_bin_count) or
                    (not model.config.ability_delay and delay != -1)):
                raise ValueError("positive teacher mass on a masked ability action")
        step = model.evaluate_actions(context, action, **masks, validate=True)
        logs.append(step.log_prob[0])
        probabilities.append(probability)
    # Use the original masked joint log probability. Re-normalizing the learner
    # over just visited actions would remove the penalty on unexplored actions.
    return distillation_loss(
        torch.stack(logs).unsqueeze(0), torch.tensor([probabilities], device=device),
        context.value, torch.tensor([record.q_target], device=device),
        policy_weight=policy_weight, value_weight=value_weight,
    )


class TeacherObjective:
    def __init__(self, records, *, policy_weight, value_weight, batch_size=8, seed=1755):
        if not records or batch_size < 1:
            raise ValueError("teacher minibatch needs records and a positive size")
        self.records = list(records)
        for record in self.records:
            record.require_training_ready()
        self.policy_weight, self.value_weight = policy_weight, value_weight
        self.batch_size, self.rng = min(batch_size, len(records)), random.Random(seed)
        self.metrics = dict(records=0, batches=0, policy_ce_sum=0., value_huber_sum=0.)

    def __call__(self, model):
        self.metrics["batches"] += 1
        for record in self.rng.sample(self.records, self.batch_size):
            loss, ce, huber, _ = teacher_record_loss(
                model, record, policy_weight=self.policy_weight, value_weight=self.value_weight)
            self.metrics["records"] += 1
            self.metrics["policy_ce_sum"] += float(ce.detach())
            self.metrics["value_huber_sum"] += float(huber.detach())
            yield loss / self.batch_size

    def report(self):
        count = self.metrics["records"]
        return dict(records=count, batches=self.metrics["batches"],
                    policy_ce=self.metrics["policy_ce_sum"] / max(1, count),
                    value_huber=self.metrics["value_huber_sum"] / max(1, count),
                    policy_weight=self.policy_weight, value_weight=self.value_weight)
