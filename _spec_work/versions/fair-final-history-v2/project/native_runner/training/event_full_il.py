"""Model-independent, audited full-state imitation data and joint policy loss."""
from dataclasses import asdict
import hashlib
import io
import json

import torch
import torch.nn.functional as F
import zstandard

from .event_abilities import candidate_ability_mask
from .event_ability_il_data import replay_partition
from .event_cache import _pack_observations
from .event_config import EventPolicyConfigV1
from .event_model import _smoothed_delay_target
from .v4.cache import _restore_record, _restore_dynamic_observation
from .v4.tensors import UniversalSemanticBatchV4

SCHEMA = "event-full-state-il.v1"


def contract(*, ability_delay=True):
    return dict(schema=SCHEMA, event_config=asdict(EventPolicyConfigV1.three_second()),
        input_semantics="owner-neutral.v1", snapshot_only=True,
        action_semantics=("WAIT/PLAY(card,cell,delay)/ABILITY(source,delay)" if ability_delay
                          else "WAIT/PLAY(card,cell,delay)/ABILITY(source,immediate)"),
        ability_delay=ability_delay, sampling="public-executed-event-or-owner-wait-deadline.v1",
        cancelled_decisions="audit-only", terminal_priority=True, weights_required=False)


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_row(row, frame, expected_contract):
    if row['partition'] != replay_partition(row['replay_tag']) or row['owner'] not in (0, 1):
        raise ValueError('whole-replay split or owner mismatch')
    if len(row['payload_sha256']) != 64 or row.get('trusted') is not True:
        raise ValueError('missing replay provenance')
    gate = row['gate']
    if gate not in (0, 1, 2) or row.get('cancelled') or row['tick'] < 0:
        raise ValueError('invalid or cancelled supervised decision')
    if gate == 0:
        if row['wait_deadline'] != row['tick'] + 60:
            raise ValueError('WAIT must wake three seconds after its own decision')
        return
    if not 0 <= row['delay_bin'] <= 30 or row['delay_ms'] != row['delay_bin'] * 100:
        raise ValueError('timing label outside the three-second contract')
    if not 0 <= row['command_tick'] - row['tick'] <= 60:
        raise ValueError('expert command outside action horizon')
    if gate == 1:
        card, cell = row['card_row'], row['cell']
        if not 0 <= card < frame.own_cards.mask.shape[1] or not 0 <= cell < 576:
            raise ValueError('invalid card or model-relative cell')
        if not row['card_mask'][card] or not row['cell_masks'][card][cell] or not row['delay_masks'][card][row['delay_bin']]:
            raise ValueError('target action not legal in recorded snapshot')
    else:
        candidate = row['ability_candidate']
        allowed = candidate_ability_mask(frame)[0]
        if not 0 <= candidate < allowed.numel() or not allowed[candidate]:
            raise ValueError('skill target absent from original native-legal snapshot')
        receipt = row['receipt']
        if (receipt['event_type'] != 'action_executed' or receipt['owner'] != row['owner']
                or receipt['entity_id'] != int(frame.candidates.native_source_entity[0, candidate])
                or receipt['tick'] != row['command_tick'] + 1
                or receipt['data'].get('ability_id') != row['ability_id']
                or receipt['data'].get('kind') != 'activate_ability'
                or receipt['data'].get('fair_ability_activation_exact') is not True):
            raise ValueError('skill target lacks exact native execution proof')
        if not expected_contract['ability_delay'] and row['delay_bin'] != 0:
            raise ValueError('immediate skill cannot carry a delayed target')


def pack(frames, rows, evidence, *, ability_delay=True):
    config = contract(ability_delay=ability_delay)
    if len(frames) != len(rows) or not rows or evidence.get('terminal_fidelity') is not True:
        raise ValueError('complete native verification required before publishing')
    for frame, row in zip(frames, rows, strict=True):
        validate_row(row, frame, config)
    tensors, _ = _pack_observations(frames, torch.float16)
    payload = dict(schema=SCHEMA, contract=config, contract_sha256=identity(config),
                   rows=rows, tensors=tensors, evidence=evidence)
    stream = io.BytesIO()
    torch.save(payload, stream)
    return zstandard.ZstdCompressor(level=6, threads=1).compress(stream.getvalue())


def unpack(blob, *, expected_contract=None):
    raw = zstandard.ZstdDecompressor().decompress(blob, max_output_size=1024**3)
    payload = torch.load(io.BytesIO(raw), map_location='cpu', weights_only=True)
    actual = payload.get('contract')
    expected = expected_contract or contract()
    # JSON normalization treats saved tuples and configured lists identically.
    if payload.get('schema') != SCHEMA or identity(actual) != identity(expected) or payload.get('contract_sha256') != identity(expected):
        raise ValueError('full IL contract mismatch; encoded skill features are not full inputs')
    if payload['evidence'].get('terminal_fidelity') is not True:
        raise ValueError('missing terminal fidelity')
    tensors = payload.pop('tensors')
    packed = _restore_record(UniversalSemanticBatchV4, prefix='observations', tensors=tensors)
    if packed.batch_size != len(payload['rows']):
        raise ValueError('frame and label count mismatch')
    frames = [_restore_dynamic_observation(packed.narrow_batch(i, 1),
              active_effect_count=int(tensors['active_effect_counts'][i]),
              relation_edge_count=int(tensors['relation_edge_counts'][i]),
              candidate_count=int(tensors['candidate_counts'][i])) for i in range(packed.batch_size)]
    for row, frame in zip(payload['rows'], frames, strict=True):
        validate_row(row, frame, expected)
    return frames, payload['rows'], payload['evidence']


def loss(model, frames, rows):
    """Every input goes through the trainable backbone, with zero recurrent state."""
    from .event_ppo import _stack_frames
    device = next(model.parameters()).device
    batch = _stack_frames(frames).to_model_input(device, float_dtype=torch.float32)
    context = model.encode(batch, model.initial_state(batch.batch_size, device=device),
                           episode_start=torch.ones(batch.batch_size, dtype=torch.bool, device=device))
    gate = torch.tensor([r['gate'] for r in rows], dtype=torch.long, device=device)
    gate_mask = torch.tensor([[True, any(r['card_mask'])] for r in rows], device=device)
    logits, allowed = model.event_gate(context, gate_mask)
    parts = {'gate': F.cross_entropy(logits.masked_fill(~allowed, torch.finfo(logits.dtype).min), gate)}
    play, skill = gate == 1, gate == 2
    card = torch.tensor([r.get('card_row', 0) for r in rows], device=device)
    cell = torch.tensor([r.get('cell', 0) for r in rows], device=device)
    source = torch.tensor([r.get('ability_candidate', -1) for r in rows], device=device)
    zero = parts['gate'] * 0
    for name in ('card', 'cell', 'delay', 'ability'):
        parts[name] = zero
    if play.any():
        card_logits = model.card_logits(context)
        mask = torch.tensor([r['card_mask'] for r in rows], device=device)
        parts['card'] = F.cross_entropy(card_logits[play].masked_fill(~mask[play], -1e9), card[play])
        cell_logits = model.cell_logits(context, card)
        cells = torch.tensor([r['cell_masks'] for r in rows], device=device)
        selected = cells[torch.arange(len(rows), device=device), card]
        parts['cell'] = F.cross_entropy(cell_logits[play].masked_fill(~selected[play], -1e9), cell[play])
    if skill.any():
        distribution = model.ability_distribution(context, 1.)
        parts['ability'] = -distribution.log_prob(source.clamp_min(0))[skill].mean()
    timed = play | skill if model.config.ability_delay else play
    if timed.any():
        delays = torch.tensor([r['delay_masks'] for r in rows], device=device)
        distribution = model.action_delay_distribution(context, card, cell, source, delays, 1.)
        targets = torch.tensor([r.get('delay_bin', 0) for r in rows], device=device)
        smooth = _smoothed_delay_target(targets[timed], model.event_config.delay_bin_count,
                                        model.config.delay_neighbor_weight)
        # Renormalize smoothing at illegal affordability bins.
        log_probs = distribution.logits[timed]
        smooth = smooth * (log_probs > -1e8)
        smooth = smooth / smooth.sum(-1, keepdim=True).clamp_min(1e-12)
        parts['delay'] = -(smooth * log_probs).sum(-1).mean()
    return sum(parts.values()), parts
