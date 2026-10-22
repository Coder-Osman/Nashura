"""Immediate, exact-source ability events for the event policy.

The native tensorizer already stores ready ability candidates, costs and source
identities in each observation. Reuse that snapshot for sampling and PPO; never
recompute historical legality from the current engine or infer it from a deck.
"""
from __future__ import annotations

import torch

from ..contracts import ActionKind, ActionV1
from .v4.tensors import CANDIDATE_ABILITY, TARGET_NONE

EVENT_ABILITY_ACTION_SEMANTICS = "model-relative-cell-and-native-ability-event.v1"


def candidate_ability_mask(frame):
    c = frame.candidates
    return (c.mask & (c.variant == CANDIDATE_ABILITY) & (c.target_mode == TARGET_NONE)
            & (c.native_source_entity >= 0) & (c.ability_vocab_id > 0)
            & torch.isfinite(c.cost) & (c.cost >= 0) & (c.cost <= c.elixir[:, None]))


def ability_plan(frame, candidate: int, *, owner: int, tick: int, ability_catalog, delay_ms: int = 0):
    allowed = candidate_ability_mask(frame)
    if not 0 <= candidate < allowed.shape[1] or not bool(allowed[0, candidate]):
        raise ValueError("selected event ability is not native-legal")
    c = frame.candidates
    if type(delay_ms) is not int or not 0 <= delay_ms <= 3000 or delay_ms % 100:
        raise ValueError("ability delay must be a 0..3000ms 100ms bin")
    vocab = int(c.ability_vocab_id[0, candidate])
    if not 1 <= vocab <= len(ability_catalog.ability_ids):
        raise ValueError("unknown event ability identity")
    return dict(kind=ActionKind.ACTIVATE_ABILITY.value, owner=owner,
                ability_candidate=candidate, source_entity=int(c.native_source_entity[0, candidate]),
                ability_id=ability_catalog.ability_ids[vocab - 1],
                decided_tick=tick, scheduled_tick=tick + delay_ms // 50, delay_ms=delay_ms)


def ability_action_from_plan(environment, observation, owner, plan, *, action_id):
    from .event_action_space import mask_decision_ready
    from .v4.native_actions import ability_runtime_contract
    mask = environment.action_mask(owner)
    source = plan["source_entity"]
    if (plan["owner"] != owner or observation.owner != owner or not mask_decision_ready(mask)
            or not mask.kinds.get(ActionKind.ACTIVATE_ABILITY.value, False)
            or source not in mask.ability_sources):
        raise ValueError("planned ability source is no longer ready")
    player = next(p for p in observation.players if p.owner == owner)
    runtime = ability_runtime_contract(observation, source_entity=source, deck=player.deck)
    if runtime.ability_id != plan["ability_id"]:
        raise ValueError("planned ability identity changed")
    return ActionV1(owner=owner, kind=ActionKind.ACTIVATE_ABILITY,
                    source_entity=source, ability_id=runtime.ability_id,
                    execute_offset_ticks=1, next_decision_ticks=1, action_id=action_id)
