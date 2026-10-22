"""Immutable, row-aligned inputs reused throughout one event PPO iteration."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Iterator, Sequence

import torch
from torch import Tensor

from .event_model import EventActionV1
from .v4.tensors import UniversalSemanticBatchV4

if TYPE_CHECKING:
    from .event_ppo import EventLaneRolloutV1


def _tensor_bytes(value) -> int:
    if isinstance(value, Tensor):
        return value.numel() * value.element_size()
    if is_dataclass(value):
        return sum(_tensor_bytes(getattr(value, field.name)) for field in fields(value))
    return 0


def _model_revision(model):
    return (id(model), tuple((id(t), t._version, t.device, t.dtype)
                            for t in (*model.parameters(), *model.buffers())),
            tuple((id(module), getattr(module, "p", None), getattr(module, "dropout", None))
                  for module in model.modules()))


def _lane_revision(lanes):
    # Observations are immutable for this iteration. Reject replaced lanes and
    # in-place mutations of behavior data rather than silently using old masks.
    return tuple((id(lane), id(lane.observations), lane.temperatures, lane.deck_id, lane.terminal_reward,
                  None if lane.decision_ticks is None else (id(lane.decision_ticks), lane.decision_ticks._version),
                  None if lane.reward_parts is None else (id(lane.reward_parts), lane.reward_parts._version),
                  tuple((id(t), t._version) for t in (
                      lane.actions.act, lane.actions.card_row, lane.actions.cell, lane.actions.delay_bin,
                      lane.actions.ability_candidate,
                      lane.old_log_prob, lane.reward, lane.done, lane.truncated, lane.gate_mask,
                      lane.card_mask, lane.cell_masks, lane.delay_mask))) for lane in lanes)


@dataclass
class EventRowBatch:
    start: int
    stop: int
    row_ids: tuple[tuple[int, int], ...]
    frames: tuple[UniversalSemanticBatchV4, ...]
    actions: EventActionV1
    old_log_prob: Tensor
    gate_mask: Tensor
    card_mask: Tensor
    cell_masks: Tensor
    delay_mask: Tensor
    temperatures: tuple[float, float, float, float]
    episode_reward: Tensor
    packed: UniversalSemanticBatchV4 | None = None
    deck_ids: tuple[str, ...] = ()

    flat_indices: Tensor | None = None
    packed_bytes: int = 0
    transfers: int = 0
    transfer_bytes: int = 0

    def select(self, values):
        return values[self.start:self.stop] if self.flat_indices is None else values.index_select(0, self.flat_indices)

    def encode(self, model):
        from .event_ppo import _encode_rows
        if self.packed is None:
            return _encode_rows(model, self.frames)
        device = next(model.parameters()).device
        if self.packed.match_scalars.device != device:
            self.transfers += 1
            self.transfer_bytes += self.packed_bytes
        batch = self.packed.to_model_input(device, float_dtype=torch.float32)
        state = model.initial_state(batch.batch_size, device=device)
        return model.encode(batch, state, episode_start=torch.ones(
            batch.batch_size, dtype=torch.bool, device=device))

    def evaluate(self, model, context):
        return model.evaluate_actions(
            context, self.actions, gate_mask=self.gate_mask, card_mask=self.card_mask,
            cell_masks=self.cell_masks, delay_mask=self.delay_mask, temperatures=self.temperatures)


def row_batches(lanes: Sequence[EventLaneRolloutV1], *, chunk_steps: int,
                lane_start: int = 0, lane_stop: int | None = None,
                cross_lane: bool = False) -> Iterator[EventRowBatch]:
    """Contiguous rows, never crossing a temperature or optimizer boundary."""
    if chunk_steps < 1:
        raise ValueError("row batch size must be positive")
    lane_stop = len(lanes) if lane_stop is None else lane_stop
    cursor = sum(lane.time_steps for lane in lanes[:lane_start])
    fragments = []
    count = 0

    def build():
        def cat(name):
            return torch.cat([getattr(lanes[i], name)[a:b] for i, a, b in fragments]).detach()
        actions = EventActionV1(**{name: torch.cat([
            getattr(lanes[i].actions, name)[a:b] for i, a, b in fragments]).detach()
            for name in ("act", "card_row", "cell", "delay_bin", "ability_candidate")})
        return EventRowBatch(
            start=cursor - count, stop=cursor,
            row_ids=tuple((i, row) for i, a, b in fragments for row in range(a, b)),
            frames=tuple(frame for i, a, b in fragments for frame in lanes[i].observations[a:b]),
            actions=actions, old_log_prob=cat("old_log_prob"), gate_mask=cat("gate_mask"),
            card_mask=cat("card_mask"), cell_masks=cat("cell_masks"), delay_mask=cat("delay_mask"),
            temperatures=lanes[fragments[0][0]].temperatures,
            deck_ids=tuple(lanes[i].deck_id for i, a, b in fragments for _ in range(a, b)),
            episode_reward=torch.cat([lanes[i].reward.new_full((b-a,), lanes[i].outcome_reward)
                                      for i, a, b in fragments]))

    for index in range(lane_start, lane_stop):
        lane = lanes[index]
        if fragments and lane.temperatures != lanes[fragments[0][0]].temperatures:
            yield build()
            fragments, count = [], 0
        start = 0
        while start < lane.time_steps:
            stop = min(lane.time_steps, start + chunk_steps - count)
            fragments.append((index, start, stop))
            count += stop - start
            cursor += stop - start
            start = stop
            if count == chunk_steps:
                yield build()
                fragments, count = [], 0
        if fragments and not cross_lane:
            yield build()
            fragments, count = [], 0
    if fragments:
        yield build()


class PreparedEventRollout:
    """One-iteration cache; optimizer groups and lane ordering stay unchanged.

    Pack once on CPU, retaining as many packed inputs on the model device as
    the byte budget permits. Overflow batches reuse CPU packing and transfer
    on demand. No activation graphs or policy outputs are cached for backward.
    """

    def __init__(self, model, lanes: Sequence[EventLaneRolloutV1], *, row_batch_size: int = 128,
                 minibatch_lanes: int = 0, device_cache_bytes: int = 512 * 1024**2,
                 bucket_by_entities: bool = True):
        from .event_ppo import _lanes_on, _stack_frames
        if row_batch_size < 1 or minibatch_lanes < 0 or device_cache_bytes < 0 or not lanes:
            raise ValueError("invalid prepared rollout batch size, lane count or cache budget")
        for lane in lanes:
            lane.validate()
        self.source_revision = _lane_revision(lanes)
        self.device = next(model.parameters()).device
        self.lanes = _lanes_on(lanes, self.device)
        self.minibatch_lanes = minibatch_lanes or len(lanes)
        self.groups = {}
        self.bucket_by_entities = bucket_by_entities
        self.dynamic_slots = self.padded_dynamic_slots = 0
        self.initial_transfer_bytes = 0
        self.batches = []
        self.packed_bytes = self.device_cached_bytes = 0
        self.reused_values = False
        self.clear_values()
        for start in range(0, len(lanes), self.minibatch_lanes):
            batches = list(row_batches(self.lanes, chunk_steps=row_batch_size, lane_start=start,
                                      lane_stop=min(len(lanes), start+self.minibatch_lanes), cross_lane=True))
            if bucket_by_entities:
                batches = bucket_rows(self.lanes, batches, row_batch_size)
            for batch in batches:
                packed = _stack_frames(batch.frames).to_storage("cpu", float_dtype=torch.float32)
                size = _tensor_bytes(packed)
                self.packed_bytes += size
                batch.packed_bytes = size
                for name in ("active_effects", "relation_edges", "candidates"):
                    self.dynamic_slots += sum(getattr(f, name).mask.numel() for f in batch.frames)
                    self.padded_dynamic_slots += getattr(packed, name).mask.numel()
                if self.device.type == "cpu" or self.device_cached_bytes + size <= device_cache_bytes:
                    packed = packed.to(self.device)
                    if self.device.type != "cpu":
                        self.initial_transfer_bytes += size
                    self.device_cached_bytes += size
                batch.packed = packed
            self.groups[start] = batches
            self.batches.extend(batches)

    def validate(self, model, lanes, *, minibatch_lanes=None):
        if _lane_revision(lanes) != self.source_revision:
            raise ValueError("prepared rollout lanes or behavior tensors changed")
        if next(model.parameters()).device != self.device:
            raise ValueError("prepared rollout model device changed")
        if minibatch_lanes is not None and (minibatch_lanes or len(lanes)) != self.minibatch_lanes:
            raise ValueError("prepared rollout optimizer group boundaries changed")

    def clear_values(self):
        self._values = None
        self._value_revision = None

    def retain_values(self, model, values, *, component_critic=None):
        # The event encoder is deterministic in train/eval today. Refuse reuse
        # if future modules introduce dropout or running statistics.
        for module in model.modules():
            if isinstance(module, torch.nn.modules.batchnorm._BatchNorm):
                return
            if isinstance(module, torch.nn.modules.dropout._DropoutNd) and module.p > 0:
                return
            if isinstance(module, torch.nn.MultiheadAttention) and module.dropout > 0:
                return
            if isinstance(module, torch.nn.RNNBase) and module.dropout > 0:
                return
        flat = self.restore_order(values).detach().clone()
        shape = (sum(lane.time_steps for lane in self.lanes),) + (() if component_critic is None else (3,))
        if flat.shape != shape or not bool(torch.isfinite(flat).all()):
            raise ValueError("preflight values must be finite and aligned with every rollout row")
        self._values = flat
        self._value_revision = (_model_revision(model), None if component_critic is None else _model_revision(component_critic))

    def restore_order(self, values):
        """Undo packing order before GAE; adjacent rows must stay in the same lane."""
        flat = torch.cat(values)
        if not self.bucket_by_entities:
            return flat
        ids = torch.cat([batch.flat_indices for batch in self.batches])
        if flat.shape[0] != ids.numel():
            raise ValueError("packed values differ from row mapping")
        result = torch.empty_like(flat)
        result.index_copy_(0, ids, flat)
        return result

    def take_values(self, model, *, component_critic=None):
        revision = (_model_revision(model), None if component_critic is None else _model_revision(component_critic))
        values = self._values if self._value_revision == revision else None
        self.clear_values()
        self.reused_values = values is not None
        return values

    def as_dict(self):
        return dict(batches=len(self.batches), rows=sum(lane.time_steps for lane in self.lanes),
                    packed_bytes=self.packed_bytes, device_cached_bytes=self.device_cached_bytes,
                    preflight_values_reused=self.reused_values,
                    entity_buckets=self.bucket_by_entities,
                    dynamic_slots=self.dynamic_slots, padded_dynamic_slots=self.padded_dynamic_slots,
                    padding_ratio=self.padded_dynamic_slots/max(1, self.dynamic_slots),
                    initial_transfer_bytes=self.initial_transfer_bytes,
                    fallback_batches=sum(b.packed.match_scalars.device != self.device for b in self.batches),
                    repeated_transfers=sum(b.transfers for b in self.batches),
                    repeated_transfer_bytes=sum(b.transfer_bytes for b in self.batches))


def bucket_rows(lanes, batches, chunk_steps):
    """Stable shape sort within each temperature/optimizer group, with explicit row IDs.

    Entity token capacities are fixed by the observation contract. Only the
    variable effect/edge/candidate axes are padded by _stack_frames; sort those
    capacities without dropping or compacting any observations.
    """
    offsets, cursor = [], 0
    for lane in lanes:
        offsets.append(cursor)
        cursor += lane.time_steps
    runs = []
    for batch in batches:
        if not runs or runs[-1][0] != batch.temperatures:
            runs.append((batch.temperatures, []))
        runs[-1][1].extend(batch.row_ids)
    result = []
    for temperature, ids in runs:
        def shape(row):
            frame = lanes[row[0]].observations[row[1]]
            sizes = tuple(getattr(frame, name).mask.shape[1]
                          for name in ("active_effects", "relation_edges", "candidates"))
            return (sum(sizes), *sizes)
        ids.sort(key=shape)
        for start in range(0, len(ids), chunk_steps):
            rows = ids[start:start+chunk_steps]
            def cat(name, action=False):
                return torch.cat([getattr(lanes[i].actions if action else lanes[i], name)[j:j+1]
                                  for i, j in rows]).detach()
            indices = torch.tensor([offsets[i]+j for i,j in rows], device=lanes[rows[0][0]].reward.device)
            result.append(EventRowBatch(
                start=0, stop=len(rows), row_ids=tuple(rows),
                flat_indices=indices,
                frames=tuple(lanes[i].observations[j] for i,j in rows),
                actions=EventActionV1(**{name: cat(name, True) for name in
                    (f.name for f in fields(lanes[rows[0][0]].actions))}),
                old_log_prob=cat("old_log_prob"), gate_mask=cat("gate_mask"), card_mask=cat("card_mask"),
                cell_masks=cat("cell_masks"), delay_mask=cat("delay_mask"), temperatures=temperature,
                episode_reward=torch.cat([lanes[i].reward.new_full((1,), lanes[i].outcome_reward) for i,j in rows]),
                deck_ids=tuple(lanes[i].deck_id for i,j in rows)))
    return result
