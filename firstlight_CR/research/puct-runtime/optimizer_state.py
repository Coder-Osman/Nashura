"""Task-owned event-ppo-training.v1 resume contract, independent of host tools."""
from copy import deepcopy
import math

import torch


def restore_optimizer(model, payload):
    saved = payload.get("event_ppo_training")
    names = [name for name, _ in model.named_parameters()]
    if (not isinstance(saved, dict) or saved.get("version") != "event-ppo-training.v1" or
            saved.get("optimizer_type") != "AdamW" or saved.get("parameter_names") != names):
        raise ValueError("saved AdamW parameter identity or training version mismatch")
    progress = {key: saved.get(key) for key in ("iterations", "optimizer_steps", "decision_rows")}
    if any(type(value) is not int or value < 0 for value in progress.values()):
        raise ValueError("saved optimizer counters must be nonnegative integers")
    state = saved.get("optimizer")
    if not isinstance(state, dict) or not state.get("state") or not state.get("param_groups"):
        raise ValueError("saved optimizer moments and parameter groups are required")
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5)
    optimizer.load_state_dict(state)
    for group in optimizer.param_groups:
        if not math.isfinite(group["lr"]) or group["lr"] <= 0:
            raise ValueError("saved learning rate must be positive and finite")
    for parameter, values in optimizer.state.items():
        for name, value in values.items():
            if isinstance(value, torch.Tensor):
                if not bool(torch.isfinite(value).all()):
                    raise ValueError("nonfinite saved optimizer state")
                if name in {"exp_avg", "exp_avg_sq", "max_exp_avg_sq"} and value.shape != parameter.shape:
                    raise ValueError("saved optimizer moment shape mismatch")
    return optimizer, progress


def optimizer_checkpoint(model, optimizer, progress, stats):
    return dict(version="event-ppo-training.v1", optimizer_type="AdamW",
                parameter_names=[name for name, _ in model.named_parameters()],
                optimizer=optimizer.state_dict(), iterations=progress["iterations"] + 1,
                optimizer_steps=progress["optimizer_steps"] + stats.optimizer_steps,
                decision_rows=progress["decision_rows"] + stats.steps)


def optimizer_signature(payload):
    saved = payload["event_ppo_training"]
    return dict(names=list(saved["parameter_names"]), groups=deepcopy(saved["optimizer"]["param_groups"]),
                state={key: dict(step=float(value.get("step", 0)),
                                 shapes={name: tuple(tensor.shape) for name, tensor in value.items()
                                         if name in {"exp_avg", "exp_avg_sq", "max_exp_avg_sq"}})
                       for key, value in saved["optimizer"]["state"].items()})


def verify_optimizer_signature(parent, child, steps, *, preserve_groups=True):
    if parent["names"] != child["names"] or (preserve_groups and parent["groups"] != child["groups"]):
        raise ValueError("optimizer parameters or saved group options changed")
    advanced = 0
    for key, old in parent["state"].items():
        new = child["state"].get(key)
        if new is None or new["shapes"] != old["shapes"]:
            raise ValueError("optimizer moments disappeared or changed shape")
        delta = new["step"] - old["step"]
        if not math.isfinite(delta) or delta < 0 or delta > steps or delta != int(delta):
            raise ValueError("optimizer per-parameter step continuity mismatch")
        advanced += int(delta > 0)
    for key, new in child["state"].items():
        if key not in parent["state"] and not 0 <= new["step"] <= steps:
            raise ValueError("new optimizer state has an invalid step")
    return advanced
