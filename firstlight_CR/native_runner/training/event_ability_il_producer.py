"""Rebuild expert skill frames with exact-source native execution evidence.

Raw public skill markers have no authoritative entity identity. This producer
accepts only a unique native-ready source at the original command boundary.
It never uses the older newest-carrier heuristic as a supervised source label.
"""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from collections import Counter
import hashlib
import json

import torch

from ..contracts import ActionKind, ActionV1
from ..royaleapi_replay import ROYALAPI_COMMAND_TO_NATIVE_OBSERVABLE_TICKS
from .event_abilities import ability_action_from_plan, ability_plan, candidate_ability_mask
from .event_ability_il import ability_il_features
from .event_ability_il_data import replay_partition, validate_label
from .event_action_space import legal_event_actions
from .v4.expert import build_expert_action_batch
from .v4.producer import _build_il_lane, _initialize_il_lane, _wait_group


def native_expert_timeline(expert_actions, commands):
    """Index labels by native execution, not the preceding source command tick."""
    by_tick = {}
    expected = Counter()
    for timed in expert_actions:
        tick = timed.action.metadata.get('native_observable_tick')
        if (type(tick) is not int or
                tick != timed.source_tick + ROYALAPI_COMMAND_TO_NATIVE_OBSERVABLE_TICKS):
            raise ValueError('expert command lacks a verified native execution boundary')
        by_tick.setdefault(tick, []).append(timed)
        expected[tick, timed.owner, timed.action.kind is ActionKind.ACTIVATE_ABILITY] += 1
    actual = Counter((command.target_tick, command.owner, command.ability) for command in commands)
    if expected != actual:
        raise ValueError('expert labels and native replay commands disagree')
    return by_tick


def skill_policy_atomic_view(result):
    """Project the known reward-only extension out of the policy envelope.

    The existing settlement probe adds an inventory for its reward accountant.
    It is not a policy input. Verify its capture identity before applying the
    ordinary strict policy validator; never suppress unknown schema fields.
    """
    rich = result.get('rich', {})
    inventory = rich.get('settlementInventory')
    if inventory is None:
        return result
    if (not isinstance(inventory, Mapping) or inventory.get('schema') != 'native-settlement-inventory.v1'
            or any(type(inventory.get(key)) is not int or inventory[key] != rich.get(key)
                   for key in ('generation', 'stateEpoch', 'tick'))):
        raise ValueError('reward inventory capture identity mismatch')
    policy_rich = dict(rich)
    del policy_rich['settlementInventory']
    return dict(result, rich=policy_rich)


@torch.no_grad()
def produce_ability_features(environment, prepared, tensorizers, model, *, payload: dict,
                             negative_stride: int = 20, progress=None):
    if negative_stride < 1:
        raise ValueError('negative stride must be positive')
    if not model.backbone.config.snapshot_only:
        raise ValueError('skill feature capture currently requires the verified snapshot-only parent')
    if model.training or not model.config.ability_actions:
        raise ValueError('capture requires an eval-mode skill model')
    device = next(model.parameters()).device
    lane = _build_il_lane(environment, prepared, tensorizers)
    commands = lane.replay_commands
    # Cards retain the existing semantic scheduler. Skills use the strict
    # BattleEnv source path, which attests the native cooldown/charge edge.
    lane.replay_commands = tuple(command for command in commands if not command.ability)
    by_tick = native_expert_timeline(lane.expert_actions, commands)
    _initialize_il_lane(lane)
    checkpoints = {lane.source_end_tick}
    checkpoints.update(range(lane.current_tick, lane.source_end_tick, negative_stride))
    for command in commands:
        checkpoints.update((command.target_tick - 1, command.target_tick, command.target_tick + 1))
    checkpoints = sorted(tick for tick in checkpoints if lane.current_tick <= tick <= lane.source_end_tick)
    payload_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    base = dict(replay_tag=prepared.replay_tag, payload_sha256=payload_sha,
                partition=replay_partition(prepared.replay_tag), trusted=True,
                sampling='native-command-boundaries-plus-periodic-negatives', negative_stride=negative_stride)
    features, records, receipts = [], [], []
    card_cursor = 0
    checkpoint_cursor = 0
    last_progress_tick = -400
    while lane.current_tick < lane.source_end_tick:
        current = lane.current_tick
        if progress is not None and current - last_progress_tick >= 400:
            progress(dict(tick=current, end_tick=lane.source_end_tick,
                          rows=len(records), verified_skills=len(receipts)))
            last_progress_tick = current
        while checkpoint_cursor < len(checkpoints) and checkpoints[checkpoint_cursor] <= current:
            checkpoint_cursor += 1
        target_tick = checkpoints[checkpoint_cursor] if checkpoint_cursor < len(checkpoints) else lane.source_end_tick
        upcoming = by_tick.get(current + 1, [])
        skill_by_owner = {}
        for timed in upcoming:
            if timed.action.kind is ActionKind.ACTIVATE_ABILITY:
                if timed.owner in skill_by_owner or sum(item.owner == timed.owner for item in upcoming) != 1:
                    raise ValueError('simultaneous expert actions make the exact skill boundary ambiguous')
                skill_by_owner[timed.owner] = timed
        native_sources_by_owner = {owner: set(environment.action_mask(owner).ability_sources) for owner in (0, 1)}
        frames = {owner: tensorizers[owner].tensorize(lane.observations[owner], validate=False)
                  for owner in (0, 1) if native_sources_by_owner[owner] or any(t.owner == owner for t in upcoming)}
        actions = _wait_group(target_tick - current)
        pending = []
        for owner in (0, 1):
            if owner not in frames:
                continue
            frame = frames[owner]
            candidates = candidate_ability_mask(frame)[0].nonzero().flatten().tolist()
            native_sources = native_sources_by_owner[owner]
            captured_sources = {int(frame.candidates.native_source_entity[0, index]) for index in candidates}
            if native_sources != captured_sources:
                raise ValueError('native skill sources were truncated or changed during tensorization')
            timed = skill_by_owner.get(owner)
            if timed is not None and len(candidates) != 1:
                raise ValueError(f'expert skill source is not unique and ready: tick={current} owner={owner} count={len(candidates)}')
            if not candidates:
                continue
            target = candidates[0] if timed is not None else -1
            legal_sources = []
            for candidate in candidates:
                plan = ability_plan(frame, candidate, owner=owner, tick=current,
                                    ability_catalog=model.backbone.ability_catalog)
                legal_sources.append({key: plan[key] for key in ('source_entity','ability_id')}
                                     | dict(candidate=candidate))
            if timed is not None:
                plan = ability_plan(frame, target, owner=owner, tick=current,
                                    ability_catalog=model.backbone.ability_catalog)
                exact = ability_action_from_plan(environment, lane.observations[owner], owner, plan,
                    action_id=f'ability-il:{prepared.replay_tag}:{timed.source_index}:{owner}')
                actions[owner] = (exact,)
            legal = legal_event_actions(environment, lane.observations[owner], frame, owner,
                catalog=model.backbone.catalog, card_count=model.card_count, cell_count=model.cell_count,
                board_width=model.event_config.board_width, board_height=model.event_config.board_height)
            context = model.encode(frame.to_model_input(device, float_dtype=torch.float32), model.initial_state(1, device=device))
            cached = ability_il_features(model, context,
                old_gate_mask=torch.tensor([[True, legal.act_allowed]], dtype=torch.bool, device=device),
                target=torch.tensor([target], dtype=torch.int64, device=device)).to('cpu')
            row = dict(base, owner=owner, tick=current, legal_sources=legal_sources,
                       expert_skill_next_tick=timed is not None)
            if timed is not None:
                row.update(source_resolution='unique-native-ready', source_index=timed.source_index,
                           action_id=exact.action_id)
            pending.append((cached, row))
        # Teacher-force only actions about to execute, with the exact source
        # used above. A failed action invalidates the entire replay shard.
        for timed in upcoming:
            owner = timed.owner
            if timed.action.kind is ActionKind.ACTIVATE_ABILITY:
                action = actions[owner][0]
                timed = replace(timed, action=replace(action, metadata={}))
            sequence = build_expert_action_batch(frames[owner], ((timed,),),
                decision_ticks_by_row=(current,), perspectives=(tensorizers[owner].perspective,),
                config=tensorizers[owner].config, validate=False)
            tensorizers[owner].record_action(sequence, frames[owner], row=0, validate=False)
        observations, _, terminated, truncated, _ = environment.step(actions, record_trace=False)
        next_tick = observations[0].tick
        if next_tick <= current or next_tick > target_tick:
            raise ValueError(f'native replay missed a requested boundary: {current}->{next_tick}, target={target_tick}')
        for cached, row in pending:
            if row['expert_skill_next_tick']:
                events = [event for event in observations[row['owner']].events
                          if event.data.get('action_id') == row['action_id'] and event.event_type == 'action_executed']
                if len(events) != 1:
                    raise ValueError('exact skill native execution was not attested on the requested tick')
                row['receipt'] = events[0].to_dict()
                receipts.append(row['receipt'])
            validate_label(row, int(cached.target[0]))
            features.append(cached)
            records.append(row)
        while card_cursor < len(lane.replay_commands) and lane.replay_commands[card_cursor].target_tick <= next_tick:
            command = lane.replay_commands[card_cursor]
            receipt = environment.native.replay_schedule_status(lane.replay_schedule_sequences[card_cursor])
            if receipt['state'] != 'succeeded' or receipt.get('queuedAtTick') != command.target_tick - 1:
                raise ValueError(f'expert card execution failed: {receipt}')
            card_cursor += 1
        lane.current_tick, lane.observations = int(next_tick), observations
        if terminated[0] or truncated[0]:
            if any(command.target_tick > next_tick for command in commands):
                raise ValueError('native replay ended before the recorded actions')
            break
    if len(receipts) != sum(command.ability for command in commands) or card_cursor != len(lane.replay_commands):
        raise ValueError('not every recorded expert command has a verified native execution receipt')
    expected = prepared.replay.terminal.get('winner')
    actual = lane.observations[0].terminal.winner
    expected_crowns = {int(p['owner']): int(p['crowns']) for p in prepared.replay.terminal.get('players', ())
                       if isinstance(p, Mapping) and 'crowns' in p}
    actual_crowns = {p.owner: p.crowns for p in lane.observations[0].players}
    if expected != actual or (expected_crowns and expected_crowns != actual_crowns):
        raise ValueError(f'replay terminal fidelity mismatch: winner {actual}/{expected}, crowns {actual_crowns}/{expected_crowns}')
    return features, records, dict(replay_tag=prepared.replay_tag, rows=len(records), skills=len(receipts),
        verified_card_commands=card_cursor,
        source_end_tick=lane.source_end_tick, simulated_end_tick=lane.current_tick,
        expected_winner=expected, actual_winner=actual, crowns=actual_crowns, terminal_fidelity=True)
