"""Explicit card-only -> skill-event migration; existing weights stay intact."""
from __future__ import annotations

import copy
from dataclasses import replace

import torch

from .event_checkpoint import _build_model


def migrate_ability_checkpoint(loaded):
    source = loaded.model
    if source.config.ability_actions:
        raise ValueError("checkpoint already supports ability events")
    target = _build_model(source.backbone.config, replace(source.config, ability_actions=True), source.event_config)
    missing, unexpected = target.load_state_dict(source.state_dict(), strict=False)
    expected = {"ability_gate.weight", "ability_gate.bias", "ability_query.weight", "ability_query.bias"}
    if set(missing) != expected or unexpected:
        raise ValueError("ability migration changed existing parameter shapes or names")
    metadata = dict(schema="event-ability-migration.v1",
        source_checkpoint_sha256=loaded.checkpoint_sha256,
        source_contract_sha256=loaded.contract_sha256,
        fresh_parameter_names=sorted(expected),
        action_semantics="WAIT / PLAY(card, cell, delay) / ABILITY(exact source, immediate)")
    extra = {key: copy.deepcopy(loaded.payload[key]) for key in (
        "kl_coefficient", "mixed_deck_controller", "event_component_gae",
        "rl_gae_config", "rl_reward_config", "rl_component_transition",
        "rl_action_constraints", "rl_execution_credit_contract",
        "rl_steps", "rl_reward_components", "rl_execution_counts", "rl_mixed_opponents",
        "policy_version", "parent_checkpoint_sha256",
    ) if key in loaded.payload}
    extra["architecture_migration"] = metadata
    saved = loaded.payload.get("event_ppo_training")
    if saved is not None:
        old, new = dict(source.named_parameters()), dict(target.named_parameters())
        optimizer = saved.get("optimizer", {})
        groups = optimizer.get("param_groups", [])
        if (saved.get("optimizer_type") != "AdamW" or saved.get("parameter_names") != list(old)
                or len(groups) != 1 or len(groups[0]["params"]) != len(old)):
            raise ValueError("ability migration requires an exactly named single-group AdamW state")
        ids = dict(zip(old, groups[0]["params"], strict=True))
        states = {}
        for i, (name, parameter) in enumerate(new.items()):
            state = optimizer["state"].get(ids.get(name))
            if state is None:
                continue
            for key, value in state.items():
                if isinstance(value, torch.Tensor) and key != "step" and value.shape != parameter.shape:
                    raise ValueError(f"AdamW shape mismatch: {name}/{key}")
            states[i] = copy.deepcopy(state)
        group = copy.deepcopy(groups[0])
        group["params"] = list(range(len(new)))
        progress = copy.deepcopy(saved)
        progress.update(parameter_names=list(new), optimizer={"state": states, "param_groups": [group]})
        extra["event_ppo_training"] = progress
        metadata["copied_adamw_states"] = len(states)
        metadata["optimizer_mapping"] = "existing moments and KL controller copied; new heads start fresh"
    target.event_initialization = dict(
        migrated_from=metadata, needs_recalibration_or_finetune=True, needs_evaluation=True)
    return target, extra
