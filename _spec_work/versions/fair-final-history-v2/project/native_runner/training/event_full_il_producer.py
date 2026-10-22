"""Teacher-forced native replays with complete, model-independent decisions."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace

import torch

from ..contracts import ActionKind
from .event_abilities import ability_action_from_plan, ability_plan, candidate_ability_mask
from .event_ability_il_producer import native_expert_timeline
from .event_ability_il_data import replay_partition
from .event_action_space import legal_event_actions
from .event_config import EventPolicyConfigV1
from .event_full_il import identity, validate_row, contract
from .event_il_clock import EventILClock
from .v4.factory import production_semantic_bundle
from .v4.expert import build_expert_action_batch
from .v4.producer import _build_il_lane, _initialize_il_lane, _wait_group


def produce_full_observations(environment, prepared, tensorizers, *, payload,
                              ability_delay=True, progress=None):
    config = EventPolicyConfigV1.three_second()
    bundle = production_semantic_bundle()
    lane = _build_il_lane(environment, prepared, tensorizers)
    commands = lane.replay_commands
    lane.replay_commands = tuple(c for c in commands if not c.ability)
    by_tick = native_expert_timeline(lane.expert_actions, commands)
    by_owner = {o: [t for t in lane.expert_actions if t.owner == o] for o in (0, 1)}
    _initialize_il_lane(lane)
    clock = EventILClock(lane.current_tick)
    boundaries = sorted({t for c in commands for t in (c.target_tick - 1, c.target_tick)})
    boundary_cursor = 0
    base = dict(replay_tag=prepared.replay_tag, payload_sha256=identity(payload),
                partition=replay_partition(prepared.replay_tag), trusted=True)
    frames, rows, excluded, receipts = [], [], [], {}
    pending = {}
    last_progress = -400
    public = False

    def finish(owner, executed_indices=(), *, terminal=False):
        item = pending.pop(owner, None)
        if item is None:
            return
        row, frame = item
        if row['gate'] == 0:
            row['next_tick'] = lane.current_tick
            frames.append(frame); rows.append(row)
        elif row['source_index'] in executed_indices:
            if row['gate'] == 2:
                receipt = receipts[row['source_index']]
                candidates = candidate_ability_mask(frame)[0]
                candidates &= frame.candidates.native_source_entity[0] == receipt['entity_id']
                matches = candidates.nonzero().flatten().tolist()
                if len(matches) != 1:
                    excluded.append(dict(owner=owner, tick=row['tick'], reason='executed_source_not_ready_at_decision'))
                    return
                row.update(ability_candidate=matches[0], receipt=receipt,
                           ability_id=receipt['data']['ability_id'])
            row['next_tick'] = lane.current_tick
            validate_row(row, frame, contract(ability_delay=ability_delay))
            frames.append(frame); rows.append(row)
        else:
            excluded.append(dict(owner=owner, tick=row['tick'], source_index=row['source_index'],
                                  reason='terminal' if terminal else 'interrupted_before_execution'))

    while lane.current_tick < lane.source_end_tick:
        now = lane.current_tick
        if progress and now - last_progress >= 400:
            progress(dict(tick=now, end_tick=lane.source_end_tick, rows=len(rows),
                          verified_skills=len(receipts), cancelled=len(excluded)))
            last_progress = now
        current_frames = {}

        def frame_for(owner):
            if owner not in current_frames:
                current_frames[owner] = tensorizers[owner].tensorize(lane.observations[owner], validate=False)
            return current_frames[owner]

        executed = [t.source_index for t in by_tick.get(now, ())]
        for owner in clock.wake(now, public=public):
            finish(owner, executed)
            frame = frame_for(owner)
            legal = legal_event_actions(environment, lane.observations[owner], frame, owner,
                catalog=bundle.card_catalog, card_count=8, cell_count=576,
                delay_offset_ms=config.delay_offset_ms, board_width=18, board_height=32)
            row = dict(base, owner=owner, tick=now, trigger='public_action' if public else 'opening' if now == clock.first_tick else 'wait_timeout',
                gate=0, wait_deadline=now + 60, card_mask=legal.card_mask.tolist(),
                cell_masks=legal.cell_masks.tolist(), delay_masks=legal.delay_masks.tolist())
            following = next((t for t in by_owner[owner] if t.source_tick >= now), None)
            if following is not None and following.source_tick - now <= 60:
                delta = following.source_tick - now
                delay_bin = config.delay_to_bin(delta * 50)
                delay_ms = config.bin_to_delay_ms(delay_bin)
                target = dict(source_index=following.source_index, command_tick=following.source_tick,
                              delay_bin=delay_bin, delay_ms=delay_ms)
                if following.action.kind is ActionKind.PLAY_CARD:
                    choices = (legal.row_card_id == following.action.card_id).nonzero().flatten().tolist()
                    if choices:
                        card = choices[0]
                        action = tensorizers[owner].perspective.action_to_model(following.action)
                        if action.target_grid is not None:
                            x, y = action.target_grid
                            cell = int(y) * 18 + int(x)
                            if (0 <= cell < 576 and legal.card_mask[card] and legal.cell_masks[card, cell]
                                    and legal.delay_masks[card, delay_bin]):
                                row.update(target, gate=1, card_row=card, cell=cell)
                elif candidate_ability_mask(frame).any() and (ability_delay or delta == 0):
                    row.update(target, gate=2)
            stored = deepcopy(frame.to_storage('cpu', float_dtype=torch.float16))
            pending[owner] = (row, stored)
            clock.decide(owner, now, source_index=row.get('source_index'),
                         execution_tick=row['command_tick'] + 1 if row['gate'] else None)

        while boundary_cursor < len(boundaries) and boundaries[boundary_cursor] <= now:
            boundary_cursor += 1
        next_boundaries = boundaries[boundary_cursor:boundary_cursor + 1]
        target_tick = clock.next_tick(now, next_boundaries, lane.source_end_tick)
        actions = _wait_group(target_tick - now)
        upcoming = by_tick.get(now + 1, ())
        skill_actions = {}
        for timed in upcoming:
            owner = timed.owner
            frame = frame_for(owner)
            if timed.action.kind is ActionKind.ACTIVATE_ABILITY:
                if sum(t.owner == owner for t in upcoming) != 1:
                    raise ValueError('simultaneous commands make the skill source ambiguous')
                allowed = candidate_ability_mask(frame)[0].nonzero().flatten().tolist()
                native_sources = set(environment.action_mask(owner).ability_sources)
                captured = {int(frame.candidates.native_source_entity[0, c]) for c in allowed}
                if native_sources != captured or len(allowed) != 1:
                    raise ValueError('expert skill source is not unique, complete and ready')
                plan = ability_plan(frame, allowed[0], owner=owner, tick=now,
                                    ability_catalog=bundle.ability_catalog)
                action = ability_action_from_plan(environment, lane.observations[owner], owner, plan,
                    action_id=f'full-il:{prepared.replay_tag}:{timed.source_index}:{owner}')
                actions[owner] = (action,)
                skill_actions[timed.source_index] = action
                timed = replace(timed, action=replace(action, metadata={}))
            sequence = build_expert_action_batch(frame, ((timed,),), decision_ticks_by_row=(now,),
                perspectives=(tensorizers[owner].perspective,), config=tensorizers[owner].config, validate=False)
            tensorizers[owner].record_action(sequence, frame, row=0, validate=False)
        observations, _, terminated, truncated, _ = environment.step(actions, record_trace=False)
        next_tick = observations[0].tick
        if not now < next_tick <= target_tick:
            raise ValueError(f'native step missed requested boundary {now}->{next_tick}/{target_tick}')
        for source_index, action in skill_actions.items():
            events = [e for e in observations[action.owner].events
                      if e.event_type == 'action_executed' and e.data.get('action_id') == action.action_id]
            if (len(events) != 1 or events[0].tick != now + 1
                    or events[0].entity_id != action.source_entity
                    or events[0].data.get('ability_id') != action.ability_id
                    or not events[0].data.get('fair_ability_activation_exact')):
                raise ValueError('skill command lacks exact next-tick execution receipt')
            receipts[source_index] = events[0].to_dict()
        lane.current_tick, lane.observations = next_tick, observations
        public = bool(by_tick.get(next_tick))
        if terminated[0] or truncated[0]:
            break

    # Publish only after all ordinary and skill commands and the terminal pass.
    for c, sequence in zip(lane.replay_commands, lane.replay_schedule_sequences, strict=True):
        receipt = environment.native.replay_schedule_status(sequence)
        if (receipt['state'] != 'succeeded' or receipt.get('queuedAtTick') != c.target_tick - 1
                or c.target_tick > lane.current_tick):
            raise ValueError(f'card native execution fidelity failed: {receipt}')
    if len(receipts) != sum(c.ability for c in commands):
        raise ValueError('missing skill execution receipts')
    expected = prepared.replay.terminal.get('winner')
    actual = lane.observations[0].terminal.winner
    crowns = {p.owner: p.crowns for p in lane.observations[0].players}
    expected_crowns = {int(p['owner']): int(p['crowns']) for p in prepared.replay.terminal.get('players', ()) if 'crowns' in p}
    if (not lane.observations[0].terminal.ended or actual != expected
            or (expected_crowns and crowns != expected_crowns)):
        raise ValueError(f'native terminal mismatch {actual}/{expected}, {crowns}/{expected_crowns}')
    for owner in (0, 1):
        finish(owner, [t.source_index for t in by_tick.get(lane.current_tick, ())], terminal=True)
    for tensorizer in tensorizers:
        tensorizer.end_episode()
    evidence = dict(schema='full-il-native-proof.v1', terminal_fidelity=True, expected_winner=expected,
        actual_winner=actual, crowns=crowns, source_end_tick=lane.source_end_tick,
        simulated_end_tick=lane.current_tick, verified_card_commands=len(lane.replay_commands),
        skills=len(receipts), skill_receipts=receipts, rows=len(rows),
        gates=dict(Counter(r['gate'] for r in rows)), triggers=dict(Counter(r['trigger'] for r in rows)),
        excluded=excluded, payload_sha256=identity(payload))
    return frames, rows, evidence
