"""Explicit migration from v2 zero-state PPO to the residual v3 architecture."""
from __future__ import annotations

import copy
from dataclasses import replace

import torch

from .event_checkpoint import _build_model
from .event_residual_core import active_gate_rows


def migrate_residual_checkpoint(loaded, *, blocks=2, hidden=512):
    source = loaded.model
    if source.config.residual_blocks or not source.backbone.config.snapshot_only:
        raise ValueError("migration requires a v2 snapshot model with zero-state decisions")
    config = replace(source.config, residual_blocks=blocks, residual_hidden=hidden)
    if blocks < 1:
        raise ValueError("migration requires at least one residual block")
    target = _build_model(source.backbone.config, config, source.event_config)
    old = dict(source.named_parameters())
    new = dict(target.named_parameters())
    rows = active_gate_rows(source.backbone.config.lstm_hidden_dim)
    sliced_names = {"backbone.lstm_core." + name for name in ("weight_ih", "bias_ih", "bias_hh")}
    fresh = []
    with torch.no_grad():
        for name, parameter in new.items():
            if name in sliced_names:
                parameter.copy_(old[name][rows])
            elif name in old:
                if parameter.shape != old[name].shape:
                    raise ValueError(f"unexpected migration shape for {name}")
                parameter.copy_(old[name])
            elif name.startswith("backbone.lstm_core.residual."):
                fresh.append(name)
            else:
                raise ValueError(f"unmapped new parameter: {name}")
        # Persistent buffers are catalogue features or identical architecture data.
        old_buffers = dict(source.named_buffers())
        for name, buffer in target.named_buffers():
            if name not in old_buffers or buffer.shape != old_buffers[name].shape:
                raise ValueError(f"unmapped migration buffer: {name}")
            buffer.copy_(old_buffers[name])
    saved = loaded.payload.get("event_ppo_training")
    if not saved or saved.get("optimizer_type") != "AdamW":
        raise ValueError("migration requires persisted AdamW PPO training state")
    if saved["parameter_names"] != list(old):
        raise ValueError("source optimizer parameter order mismatch")
    optimizer = saved["optimizer"]
    if len(optimizer["param_groups"]) != 1:
        raise ValueError("migration supports one explicitly mapped AdamW group")
    source_ids = optimizer["param_groups"][0]["params"]
    if len(source_ids) != len(old):
        raise ValueError("source optimizer IDs mismatch")
    by_name = dict(zip(old, source_ids, strict=True))
    states = {}
    for index, (name, parameter) in enumerate(new.items()):
        source_state = optimizer["state"].get(by_name.get(name))
        if source_state is None:
            continue
        mapped = {}
        for key, value in source_state.items():
            if isinstance(value, torch.Tensor):
                if key != "step":
                    if value.shape != old[name].shape:
                        raise ValueError(f"unexpected AdamW tensor {name}/{key}")
                    value = value[rows] if name in sliced_names else value
                    if value.shape != parameter.shape:
                        raise ValueError(f"target AdamW shape mismatch {name}/{key}")
                mapped[key] = value.detach().cpu().clone()
            else:
                mapped[key] = copy.deepcopy(value)
        states[index] = mapped
    group = copy.deepcopy(optimizer["param_groups"][0])
    group["params"] = list(range(len(new)))
    progress = {key: copy.deepcopy(value) for key, value in saved.items()
                if key not in {"optimizer", "parameter_names"}}
    progress.update(parameter_names=list(new), optimizer={"state": states, "param_groups": [group]})
    metadata = {
        "schema": "event-residual-migration.v1",
        "source_checkpoint_sha256": loaded.checkpoint_sha256,
        "source_contract_sha256": loaded.contract_sha256,
        "source_iterations": saved["iterations"],
        "residual_blocks": blocks, "residual_hidden": hidden,
        "source_parameters": source.parameter_count(),
        "target_parameters": target.parameter_count(),
        "sliced_parameter_names": sorted(sliced_names),
        "removed_parameter_names": sorted(set(old) - set(new)),
        "fresh_parameter_names": fresh,
        "copied_adamw_states": len(states),
        "optimizer_mapping": "unchanged tensors copied; active gate rows sliced; residual states fresh",
        "previous_action_branch": "retained to preserve optimizer parameterization",
        "state_semantics": "stateless; supplied recurrent state ignored",
    }
    return target, {"event_ppo_training": progress, "architecture_migration": metadata}
