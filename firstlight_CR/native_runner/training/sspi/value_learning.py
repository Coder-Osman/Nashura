"""Outcome-only supervision for complete, no-search SSPI Hog episodes."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random

import torch
import torch.nn.functional as F

from ..fair_search.contracts import semantic_digest
from .augmentation import deserialize_information
from .collector import VALUE_ARM, VALUE_EPISODE_SCHEMA, value_sample_gate
from .learning import WLLabel, decisive_result, validate_value_logits
from .model import SSPIModel, VALUE_ORDER


VALUE_BATCH = 128
VALUE_MICROBATCH = 4
VALIDATION_INTERVAL = 32


@dataclass(frozen=True)
class ValueRow:
    game_id: str
    information: object
    final_wl: WLLabel

    def __post_init__(self):
        if not isinstance(self.final_wl, WLLabel):
            raise ValueError("value row requires an explicit WIN/LOSS label")


def policy_head_sha(model: SSPIModel):
    return semantic_digest(model.policy_head.state_dict())


def configure_value_training(model: SSPIModel):
    for parameter in model.parameters():
        parameter.requires_grad_(True)
    for parameter in model.policy_head.parameters():
        parameter.requires_grad_(False)
    return tuple(parameter for parameter in model.parameters() if parameter.requires_grad)


def create_value_optimizer(model: SSPIModel):
    parameters = configure_value_training(model)
    return torch.optim.AdamW(parameters, lr=1e-4, weight_decay=1e-4)


def load_value_episode(directory, *, expected_kind, run_id, model_sha):
    directory = Path(directory)
    metadata = json.loads((directory / "episode.json").read_text())
    identity = metadata.get("identity") or {}
    decisive = decisive_result(metadata.get("result"))
    if (metadata.get("schema") != VALUE_EPISODE_SCHEMA
            or metadata.get("status") != "complete"
            or metadata.get("episode_kind") != expected_kind
            or metadata.get("training_admissible") is not (expected_kind == "training" and decisive)
            or metadata.get("search_roots") != 0
            or metadata.get("search_probability") != 0.
            or metadata.get("value_sample_probability") != 0.5
            or identity.get("collection_protocol") != "value-only.v2"
            or identity.get("run_id") != run_id or identity.get("arm") != VALUE_ARM
            or identity.get("model_sha256_by_owner") != {"0": model_sha, "1": model_sha}
            or identity.get("value_sample_probability") != 0.5):
        raise ValueError("value-only episode identity or natural terminal differs")
    result = metadata.get("result") or {}
    supervision = metadata.get("supervision") or {}
    if (supervision.get("value_order") != list(VALUE_ORDER)
            or supervision.get("eligible") is not decisive
            or supervision.get("excluded_reason") != (None if decisive else "natural_draw")):
        raise ValueError("value-only supervision protocol differs")
    tensor_path = directory / "episode.pt"
    action_path = directory / "actions.json"
    if (hashlib.sha256(tensor_path.read_bytes()).hexdigest() != metadata.get("tensor_sha256")
            or hashlib.sha256(action_path.read_bytes()).hexdigest() != metadata.get("actions_sha256")):
        raise ValueError("value-only episode tensor or action checksum differs")
    manifest = torch.load(tensor_path, weights_only=True, map_location="cpu")
    if (manifest.get("schema") != VALUE_EPISODE_SCHEMA
            or manifest.get("identity") != identity or manifest.get("roots") != []):
        raise ValueError("value-only episode contains a search root or mismatched identity")
    ledger = json.loads(action_path.read_text())
    actions = ledger.get("rows")
    if (not isinstance(actions, list)
            or len(actions) != sum(metadata["decisions"]["decisions"].values())
            or any(item.get("mode") != "DIRECT" for item in actions)):
        raise ValueError("value-only action ledger contains search or missing decisions")
    selected = []
    for item in actions:
        expected, gate_seed = value_sample_gate(identity["seed"], run_id=run_id,
            game_id=identity["random_stream_id"], owner=item["owner"],
            decision_index=item["decision_index"], probability=0.5)
        if (item.get("value_selected") is not expected
                or item.get("value_gate_seed") != gate_seed):
            raise ValueError("value-only sampling gate differs from the action ledger")
        if expected:
            selected.append(item)
    raw_rows = manifest.get("value_rows")
    if not decisive:
        if (raw_rows != [] or metadata.get("value_samples") != 0
                or supervision.get("discarded_value_samples") != len(selected)
                or len(selected) != sum(metadata["decisions"]["value_samples"].values())):
            raise ValueError("draw episode retained value samples or lost exclusion evidence")
        return [], metadata
    if (not isinstance(raw_rows, list) or len(raw_rows) != len(selected)
            or len(raw_rows) != metadata.get("value_samples")
            or len(raw_rows) != sum(metadata["decisions"]["value_samples"].values())):
        raise ValueError("value-only sample count differs from its action ledger")
    rows = []
    for raw, action in zip(raw_rows, selected):
        information = deserialize_information(raw)
        if (raw.get("decision_index") != action["decision_index"]
                or raw.get("tick") != action["tick"]
                or raw.get("owner") != action["owner"]
                or raw.get("fair_sha256") != action["fair_sha256"]
                or raw.get("value_gate_seed") != action["value_gate_seed"]
                or raw.get("action") != action["action"]
                or raw.get("executed_action") != action["executed_action"]
                or raw.get("wl") != result["labels"][information.owner]):
            raise ValueError("value-only FAIR row lacks matching decision or terminal evidence")
        rows.append(ValueRow(identity["game_id"], information, WLLabel[raw["wl"]]))
    return rows, metadata


def value_loss(model: SSPIModel, rows):
    if not rows:
        raise ValueError("value-only batch is empty")
    logits = torch.cat([model.value_only(row.information) for row in rows], dim=0)
    validate_value_logits(logits)
    labels = torch.tensor([int(row.final_wl) for row in rows],
        dtype=torch.long, device=logits.device)
    loss = F.cross_entropy(logits, labels)
    if not bool(torch.isfinite(loss)):
        raise ValueError("value-only WL loss is nonfinite")
    return loss


def value_predictions(model: SSPIModel, rows):
    model.eval()
    result = []
    with torch.inference_mode():
        for row in rows:
            logits = model.value_only(row.information)
            validate_value_logits(logits)
            probabilities = logits.softmax(-1)[0]
            result.append((row.game_id, int(row.final_wl),
                tuple(float(item) for item in probabilities.cpu())))
    return result


def prediction_metrics(predictions):
    if not predictions:
        raise ValueError("value prediction metrics require held-out rows")
    ce = brier = 0.
    bins = [[0., 0., 0.] for _ in range(10)]
    for _game_id, label, probabilities in predictions:
        if (type(label) is not int or label not in (0, 1) or len(probabilities) != 2
                or not all(math.isfinite(x) and 0. <= x <= 1. for x in probabilities)
                or not math.isclose(sum(probabilities), 1., rel_tol=0., abs_tol=1e-6)):
            raise ValueError("invalid WIN/LOSS prediction")
        ce -= math.log(max(probabilities[label], 1e-12))
        brier += sum((probabilities[i] - float(i == label)) ** 2 for i in range(2))
        confidence = max(probabilities)
        bin_index = min(9, int(confidence * 10))
        bins[bin_index][0] += 1.
        bins[bin_index][1] += confidence
        bins[bin_index][2] += float(probabilities.index(confidence) == label)
    count = len(predictions)
    ece = sum((group[0] / count) * abs(group[1] / group[0] - group[2] / group[0])
        for group in bins if group[0])
    return dict(rows=count, games=len({item[0] for item in predictions}),
        wl_ce=ce / count, brier=brier / count, ece10=ece)


def train_value_generation(model: SSPIModel, optimizer, training_rows, validation_rows, *, seed):
    if type(seed) is not int or type(optimizer) is not torch.optim.AdamW:
        raise ValueError("value generation requires independent train and validation rows")
    if not training_rows or not validation_rows:
        return dict(status="skipped", reason="no_effective_training_or_validation_samples",
            optimizer_steps=0, attempted_steps=0)
    parameters = configure_value_training(model)
    frozen_sha = policy_head_sha(model)
    initial_model = copy.deepcopy(model.state_dict())
    initial_optimizer = copy.deepcopy(optimizer.state_dict())
    baseline = prediction_metrics(value_predictions(model, validation_rows))
    best = None
    best_ce = baseline["wl_ce"] - 1e-5
    ordering = list(range(len(training_rows)))
    random.Random(seed).shuffle(ordering)
    checks = []
    steps = 0
    try:
        for start in range(0, len(ordering), VALUE_BATCH):
            indices = ordering[start:start + VALUE_BATCH]
            optimizer.zero_grad(set_to_none=True)
            model.train()
            for offset in range(0, len(indices), VALUE_MICROBATCH):
                micro = [training_rows[i] for i in indices[offset:offset + VALUE_MICROBATCH]]
                (value_loss(model, micro) * (len(micro) / len(indices))).backward()
            if any(parameter.grad is not None for parameter in model.policy_head.parameters()):
                raise ValueError("value-only training reached the policy head")
            norm = torch.nn.utils.clip_grad_norm_(parameters, 1.)
            if not bool(torch.isfinite(norm)):
                raise ValueError("value-only gradient norm is nonfinite")
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            steps += 1
            if steps % VALIDATION_INTERVAL == 0 or start + VALUE_BATCH >= len(ordering):
                metrics = prediction_metrics(value_predictions(model, validation_rows))
                checks.append(dict(step=steps, **metrics))
                if metrics["wl_ce"] < best_ce:
                    best_ce = metrics["wl_ce"]
                    best = (copy.deepcopy(model.state_dict()),
                        copy.deepcopy(optimizer.state_dict()), metrics, steps)
        if best is None:
            raise RuntimeError("value-only generation did not improve independent WL CE")
        model.load_state_dict(best[0])
        optimizer.load_state_dict(best[1])
        if policy_head_sha(model) != frozen_sha:
            raise ValueError("value-only update changed policy-head parameters")
        model.eval()
        return dict(status="updated", rows=len(training_rows), games=len({row.game_id for row in training_rows}),
            seed=seed, batch_size=VALUE_BATCH, microbatch=VALUE_MICROBATCH,
            optimizer_steps=best[3], attempted_steps=steps, validation_before=baseline,
            validation_after=best[2], validation_checks=checks,
            policy_head_sha=frozen_sha)
    except BaseException:
        model.load_state_dict(initial_model)
        optimizer.load_state_dict(initial_optimizer)
        optimizer.zero_grad(set_to_none=True)
        model.eval()
        raise
