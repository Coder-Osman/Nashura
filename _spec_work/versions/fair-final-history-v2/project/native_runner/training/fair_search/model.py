from copy import deepcopy
import hashlib
import io
import random
from pathlib import Path

import torch
from torch import nn

from ..event_checkpoint import (
    CONTRACT_KEY, _build_model, _resolve, event_checkpoint_payload, load_event_checkpoint,
)
from .config import PARENT_SHA256
from .contracts import require_sha
from .storage import atomic_write, torch_bytes


LEGACY_CHECKPOINT_SCHEMA = "fair-search-checkpoint.v1"
CHECKPOINT_SCHEMA = "fair-search-checkpoint.v2"
NEUTRAL_VALUE_HEAD = "linear-wdl.v1"
TRANSFERRED_VALUE_HEAD = "legacy-mlp-wdl.v1"


def _transfer_value_head(policy):
    legacy = policy.backbone.value_head
    if not isinstance(legacy, nn.Sequential) or len(legacy) != 3:
        raise ValueError("legacy value head must be Linear-SiLU-Linear")
    hidden, activation, output = legacy
    if (not isinstance(hidden, nn.Linear) or not isinstance(activation, nn.SiLU)
            or not isinstance(output, nn.Linear)
            or hidden.in_features != policy.backbone.config.lstm_hidden_dim
            or output.in_features != hidden.out_features or output.out_features != 1
            or hidden.bias is None or output.bias is None):
        raise ValueError("legacy value head architecture differs")
    if any(not bool(torch.isfinite(parameter).all()) for parameter in legacy.parameters()):
        raise ValueError("legacy value weights must be finite")
    final = nn.Linear(output.in_features, 3, device=output.weight.device, dtype=output.weight.dtype)
    with torch.no_grad():
        final.weight.zero_()
        final.bias.zero_()
        final.weight[0].copy_(output.weight[0] * .5)
        final.weight[2].copy_(output.weight[0] * -.5)
        final.bias[0].copy_(output.bias[0] * .5)
        final.bias[2].copy_(output.bias[0] * -.5)
    return nn.Sequential(deepcopy(hidden), deepcopy(activation), final)


class SearchPolicy(nn.Module):
    def __init__(self, policy, *, value_head=TRANSFERRED_VALUE_HEAD):
        super().__init__()
        if not policy.backbone.config.snapshot_only or policy.config.ability_actions:
            raise ValueError("FAIR Hog v1 requires snapshot-only card policy")
        self.policy = policy
        self.value_head_architecture = value_head
        if value_head == TRANSFERRED_VALUE_HEAD:
            self.wdl_head = _transfer_value_head(policy)
        elif value_head == NEUTRAL_VALUE_HEAD:
            self.wdl_head = nn.Linear(policy.backbone.config.lstm_hidden_dim, 3)
            nn.init.zeros_(self.wdl_head.weight)
            nn.init.zeros_(self.wdl_head.bias)
        else:
            raise ValueError("unsupported search value head architecture")

    def encode(self, frame):
        device = next(self.parameters()).device
        frame = frame.to_model_input(device, float_dtype=torch.float32)
        context = self.policy.encode(frame, self.policy.initial_state(frame.batch_size, device=device),
                                     episode_start=torch.ones(frame.batch_size, dtype=torch.bool, device=device))
        logits = self.wdl_head(context.value_context)
        return context, logits

    def value(self, logits):
        probabilities = logits.softmax(-1)
        return probabilities[..., 0] - probabilities[..., 2]


def bootstrap(path, *, device="cpu", expected_sha256=PARENT_SHA256):
    loaded = load_event_checkpoint(path, device=device)
    if loaded.checkpoint_sha256 != require_sha(expected_sha256):
        raise ValueError("warm-start checkpoint identity differs")
    return SearchPolicy(loaded.model).to(device), loaded.checkpoint_sha256


def rng_state():
    return dict(python=random.getstate(), cpu=torch.get_rng_state(),
                cuda=torch.cuda.get_rng_state_all() if torch.cuda.is_available() else [])


def restore_rng(state):
    random.setstate(state["python"])
    torch.set_rng_state(state["cpu"])
    if state["cuda"]:
        if not torch.cuda.is_available() or len(state["cuda"]) != torch.cuda.device_count():
            raise ValueError("CUDA RNG topology differs on training resume")
        torch.cuda.set_rng_state_all(state["cuda"])


def save_checkpoint(path, model, optimizer, *, generation, parent_sha256, config_sha256,
                    replay_state, sample_credit, optimizer_steps, replace=False):
    require_sha(parent_sha256)
    require_sha(config_sha256)
    payload = dict(schema=CHECKPOINT_SCHEMA, policy=event_checkpoint_payload(model.policy),
                   value_head=model.value_head_architecture,
                   wdl=model.wdl_head.state_dict(), optimizer=optimizer.state_dict(),
                   generation=generation, parent_sha256=parent_sha256, config_sha256=config_sha256,
                   replay_state=replay_state, sample_credit=sample_credit,
                   optimizer_steps=optimizer_steps, rng=rng_state())
    data = torch_bytes(payload)
    atomic_write(path, data, replace=replace)
    return hashlib.sha256(data).hexdigest()


def load_checkpoint(path, *, device="cpu", expected_sha256=None, resume=False):
    with open(path, "rb") as stream:
        data = stream.read()
    sha = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and sha != require_sha(expected_sha256):
        raise ValueError("search checkpoint file identity differs")
    payload = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    expected = {"schema", "policy", "wdl", "optimizer", "generation", "parent_sha256", "config_sha256",
                "replay_state", "sample_credit", "optimizer_steps", "rng"}
    if not isinstance(payload, dict) or payload.get("schema") not in (LEGACY_CHECKPOINT_SCHEMA, CHECKPOINT_SCHEMA):
        raise ValueError("expected independent FAIR checkpoint, not PPO")
    legacy = payload["schema"] == LEGACY_CHECKPOINT_SCHEMA
    if not legacy:
        expected.add("value_head")
    if set(payload) != expected:
        raise ValueError("search checkpoint fields differ")
    require_sha(payload["parent_sha256"])
    require_sha(payload["config_sha256"])
    backbone, model_config, event = _resolve(payload["policy"], None, None)
    policy = _build_model(backbone, model_config, event)
    policy.load_state_dict(payload["policy"]["model"], strict=True)
    if CONTRACT_KEY not in payload["policy"]:
        raise ValueError("missing event architecture identity")
    model = SearchPolicy(policy, value_head=NEUTRAL_VALUE_HEAD if legacy else payload["value_head"])
    model.wdl_head.load_state_dict(payload["wdl"], strict=True)
    model.to(device)
    if resume:
        from .replay import resolve_replay_state

        payload["replay_state"] = resolve_replay_state(payload["replay_state"], Path(path).parent)
        restore_rng(payload["rng"])
    return model, payload, sha
