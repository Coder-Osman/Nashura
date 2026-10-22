"""Restorable event-policy driver for the task's real 1755 checkpoint."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

import torch

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
for _import_path in (str(_PROJECT_ROOT), str(_RUNTIME_ROOT)):
    while _import_path in sys.path:
        sys.path.remove(_import_path)
sys.path[:0] = [str(_PROJECT_ROOT), str(_RUNTIME_ROOT)]

from native_runner.contracts import ActionV1
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.event_abilities import ability_action_from_plan, ability_plan
from native_runner.training.event_abilities import candidate_ability_mask
from native_runner.training.event_action_space import (
    event_cell_to_native, legal_event_actions, mask_decision_ready, native_hand_slots,
)
from native_runner.training.event_cache import canonicalize_event_observation
from native_runner.training.event_model import EventActionV1
from native_runner.training.event_rewards import ElixirOverflowMeter, raw_elixir, reward_config
from native_runner.training.event_rollout import EventLaneRecorderV1
from native_runner.training.event_scheduling import (
    EventExecutionLedger, complete_event_commands, gameplay_end_tick, policy_actions_open,
)
from native_runner.training.event_spell_constraints import action_constraints_contract, spell_target_allowed
from native_runner.training.event_tower_rewards import tower_state
from native_runner.training.v4.expert import POLICY_DECISION_TICKS
from native_runner.training.v4.factory import build_episode_tensorizer_v4, production_semantic_bundle
from native_runner.training.v4.model import _distribution
from policy_rollout import _EPISODE_FIELDS, _TRACKER_FIELDS, _clone, _decode, _encode, _restore_tensorizer, _tensorizer_snapshot
from recorder_capsule import (
    RecorderBranchDelta, RootRecorderCapsule, SearchRecorderState,
    fast_semantic_digest, restore_recorder, semantic_digest,
)
from packed_action import PackedActionSpace, PackedPrior

SCHEMA = "hog-event-policy-rollout.v1"
_STATE_FIELDS = (
    "plans", "ledger", "recovery_owners", "event_ticks", "seen_events",
    "last_decision_by_owner", "ready", "frames", "legals", "tick", "terminal",
    "recorder", "totals", "decisions", "steps", "cancellations", "native_events", "initial_tower_hp",
)
_SEARCH_RESTORE_CACHE_SIZE = 96
_SEARCH_VALUE_CACHE_SIZE = 256
_SEARCH_POLICY_CACHE_SIZE = 128


def _root_capsule_restore_value(name: str, value: Any) -> Any:
    if name in {"frames", "legals", "initial_tower_hp"}:
        return dict(value)
    return _clone(value)


def wait_span(tick, plans, event_ticks, last_decision_tick, max_advance, max_wait):
    bounds = [int(max_advance), max(1, int(max_wait) - (tick - int(last_decision_tick)))]
    bounds.extend(max(1, int(plan["scheduled_tick"]) - tick) for plan in plans if plan is not None)
    bounds.extend(max(1, int(when) - tick) for when in event_ticks if int(when) > tick)
    return max(1, min(bounds))


def _cell_is_legal(mask, hand_slot, cell):
    entry = mask.placement_masks.get(str(int(hand_slot)))
    if not isinstance(entry, Mapping):
        return False
    rows = entry.get("row_major")
    if not rows:
        return False
    x, y = int(cell[0]), int(cell[1])
    return 0 <= y < len(rows) and 0 <= x < len(rows[y]) and bool(rows[y][x])


from action_codec import action_key, decode_action


@dataclass(slots=True)
class SearchExecutionEntry:
    owner: int
    decision_tick: int
    action_key: str
    status: str
    cancel_reason: str | None = None
    scheduled_tick: int | None = None
    action_id: str | None = None
    submitted_action: Any = None
    closed_ref: bool = False


@dataclass(slots=True)
class SearchExecutionLedger:
    """Compact execution semantics for virtual search branches."""

    entries: list[SearchExecutionEntry]
    current_by_owner: dict[int, int]
    submitted: dict[str, int]
    action_ids: set[str]

    @classmethod
    def from_state(cls, state):
        if set(state) != {"schema", "entries", "current_by_owner", "submitted", "action_ids"} \
                or state["schema"] != "search-execution-ledger.v1":
            raise ValueError("search execution ledger schema differs")
        entries = [SearchExecutionEntry(**item) for item in state["entries"]]
        current = {int(owner): int(index) for owner, index in state["current_by_owner"].items()}
        submitted = {str(identity): int(index) for identity, index in state["submitted"].items()}
        if (any(owner not in (0, 1) or not 0 <= index < len(entries)
                for owner, index in current.items())
                or any(not identity or not 0 <= index < len(entries)
                       for identity, index in submitted.items())):
            raise ValueError("search execution ledger references an invalid entry")
        return cls(entries, current, submitted, set(state["action_ids"]))

    def to_state(self):
        return {
            "schema": "search-execution-ledger.v1",
            "entries": [{
                "owner": entry.owner,
                "decision_tick": entry.decision_tick,
                "action_key": entry.action_key,
                "status": entry.status,
                "cancel_reason": entry.cancel_reason,
                "scheduled_tick": entry.scheduled_tick,
                "action_id": entry.action_id,
                "submitted_action": entry.submitted_action,
                "closed_ref": entry.closed_ref,
            } for entry in self.entries],
            "current_by_owner": dict(self.current_by_owner),
            "submitted": dict(self.submitted),
            "action_ids": sorted(self.action_ids),
        }

    @classmethod
    def from_recorder(cls, recorder):
        ledger = cls([], {}, {}, set())
        pending_entries = {}
        for identity, pending in recorder._submitted.items():
            action_identity = str(identity)
            entry = SearchExecutionEntry(
                owner=int(pending.owner), decision_tick=int(pending.observation_tick),
                action_key=action_key(pending.action), status="submitted",
                action_id=action_identity, closed_ref=pending.closed_index is not None,
            )
            if int(pending.action.ability_candidate[0]) >= 0:
                candidate = int(pending.action.ability_candidate[0])
                entry.submitted_action = dict(
                    ability_source=int(pending.frame.candidates.native_source_entity[0, candidate]),
                )
            index = len(ledger.entries)
            ledger.entries.append(entry)
            ledger.submitted[action_identity] = index
            ledger.action_ids.add(action_identity)
            pending_entries[id(pending)] = index
        for owner, pending in recorder._pending.items():
            if pending is None:
                continue
            index = pending_entries.get(id(pending))
            if index is None:
                entry = SearchExecutionEntry(
                    owner=int(owner), decision_tick=int(pending.observation_tick),
                    action_key=action_key(pending.action),
                    status=str(pending.execution_status), action_id=pending.action_id,
                )
                index = len(ledger.entries)
                ledger.entries.append(entry)
            ledger.current_by_owner[int(owner)] = index
        return ledger

    def commit(self, owner: int, tick: int, key: str, scheduled_tick: int | None) -> None:
        owner = int(owner)
        old_index = self.current_by_owner.pop(owner, None)
        if old_index is not None:
            old = self.entries[old_index]
            if old.status == "planned":
                old.status, old.cancel_reason = "cancelled", "interrupted"
            old.closed_ref = True
        entry = SearchExecutionEntry(
            owner=owner, decision_tick=int(tick), action_key=key,
            status="planned" if scheduled_tick is not None else "executed",
            scheduled_tick=None if scheduled_tick is None else int(scheduled_tick),
        )
        index = len(self.entries)
        self.entries.append(entry)
        if scheduled_tick is not None:
            self.current_by_owner[owner] = index

    def cancel(self, owner: int, reason: str) -> bool:
        index = self.current_by_owner.get(int(owner))
        if index is None:
            return False
        entry = self.entries[index]
        if entry.status != "planned":
            return False
        entry.status, entry.cancel_reason = "cancelled", str(reason)
        self.current_by_owner.pop(int(owner), None)
        return True

    def submit(self, owner: int, tick: int, action: ActionV1) -> None:
        index = self.current_by_owner.get(int(owner))
        if index is None:
            raise ValueError("search submission has no planned action")
        entry = self.entries[index]
        identity = str(action.action_id or "")
        if (entry.status != "planned" or entry.decision_tick != int(tick)
                or not identity or identity in self.action_ids):
            raise ValueError("search submission does not identify one planned action")
        submitted_action = None
        if action.kind.value == "activate_ability":
            submitted_action = {
                "ability_source": int(action.source_entity),
                "ability_id": str(action.ability_id),
            }
        entry.status, entry.action_id, entry.submitted_action = "submitted", identity, submitted_action
        self.action_ids.add(identity)
        self.submitted[identity] = index

    def reconcile(self, outcomes) -> None:
        for event in outcomes:
            identity = event.data.get("action_id")
            index = self.submitted.get(identity)
            if index is None:
                continue
            entry = self.entries[index]
            if event.owner != entry.owner:
                raise ValueError("search execution receipt owner mismatch")
            action = entry.submitted_action
            if isinstance(action, dict):
                source = action.get("ability_source")
                if (event.event_type == "action_executed"
                        and (getattr(event, "entity_id", None) != source
                             or event.data.get("kind") != "activate_ability"
                             or (action.get("ability_id") is not None
                                 and event.data.get("ability_id") != action["ability_id"])
                             or not event.data.get("fair_ability_activation_exact"))):
                    raise ValueError("ability receipt does not attest its exact search source")
            elif action is not None and action.kind.value == "activate_ability":
                if (event.event_type == "action_executed"
                        and (event.entity_id != action.source_entity
                             or event.data.get("kind") != "activate_ability"
                             or event.data.get("ability_id") != action.ability_id
                             or not event.data.get("fair_ability_activation_exact"))):
                    raise ValueError("ability receipt does not attest its exact search source")
            if event.event_type == "action_executed":
                entry.status, entry.cancel_reason = "executed", None
            elif event.event_type in {"action_rejected", "action_canceled_terminal"}:
                entry.status, entry.cancel_reason = "cancelled", event.event_type
            else:
                raise ValueError("unexpected search execution receipt")
            entry.closed_ref = True
            del self.submitted[identity]
            if self.current_by_owner.get(entry.owner) == index:
                self.current_by_owner.pop(entry.owner)

    def close_episode(self) -> None:
        for owner, index in tuple(self.current_by_owner.items()):
            entry = self.entries[index]
            if entry.status == "planned":
                entry.status, entry.cancel_reason = "cancelled", "terminal_before_execution"
            entry.closed_ref = True
            del self.current_by_owner[owner]


@dataclass(frozen=True, slots=True)
class RootRolloutCapsule:
    """Root-only policy state used by the P13 fast restore path.

    The native adapter owns the native snapshot. This capsule owns the
    search-only logical root and is restored without JSON encoding/decoding.
    It is intentionally restricted to the search ledger path until the full
    recorder shadow contract is complete.
    """

    schema: str
    state: Mapping[str, Any]
    tensorizers: Mapping[int, Mapping[str, Any]]
    overflow: Mapping[str, Any] | None
    torch_rng: Any
    cuda_rng: Any
    digest: str | None

    @classmethod
    def from_rollout(cls, rollout: "EventPolicyRollout", *, audit_digest: bool = True
                     ) -> "RootRolloutCapsule":
        if rollout._search_execution_ledger is None:
            raise ValueError("P13 root capsule requires search-only ledger mode")
        started = time.perf_counter()
        state = {
            name: (rollout._search_execution_ledger.to_state()
                   if name == "recorder" else _clone(getattr(rollout, name)))
            for name in _STATE_FIELDS
        }
        rollout._profile_span("capsule_state_capture_ms", started)
        started = time.perf_counter()
        tensorizers = {owner: _tensorizer_snapshot(tensorizer, profile_sink=rollout.profile_sink)
                       for owner, tensorizer in rollout.tensorizers.items()}
        overflow = (None if rollout.overflow is None else
                    {name: _clone(getattr(rollout.overflow, name))
                     for name in ("started", "receipts", "owners")})
        torch_rng = torch.get_rng_state().clone()
        cuda_rng = (torch.cuda.get_rng_state(rollout.device).clone()
                    if rollout.device.type == "cuda" else None)
        rollout._profile_span("capsule_tensorizer_capture_ms", started)
        started = time.perf_counter()
        digest = (semantic_digest({
            "schema": "root-rollout-capsule.v1", "state": state,
            "tensorizers": tensorizers, "overflow": overflow,
            "torch_rng": torch_rng, "cuda_rng": cuda_rng,
        }) if audit_digest else None)
        rollout._profile_span("capsule_digest_capture_ms", started)
        return cls("root-rollout-capsule.v1", state, tensorizers, overflow,
                   torch_rng, cuda_rng, digest)

    def audit(self) -> None:
        if self.digest is None:
            return
        actual = semantic_digest({
            "schema": self.schema, "state": self.state,
            "tensorizers": self.tensorizers, "overflow": self.overflow,
            "torch_rng": self.torch_rng, "cuda_rng": self.cuda_rng,
        })
        if actual != self.digest:
            raise ValueError("P13 root rollout capsule was mutated")

    def restore_into(self, rollout: "EventPolicyRollout", *, state_epoch: int | None = None,
                     verify_digest: bool = True) -> None:
        if verify_digest:
            started = time.perf_counter()
            self.audit()
            rollout._profile_span("capsule_audit_restore_ms", started)
        if self.schema != "root-rollout-capsule.v1":
            raise ValueError("root rollout capsule schema differs")
        started = time.perf_counter()
        for name, value in self.state.items():
            if name == "recorder":
                rollout._search_execution_ledger = SearchExecutionLedger.from_state(value)
            else:
                setattr(rollout, name, _root_capsule_restore_value(name, value))
        rollout._rebind_seen_event_epoch(state_epoch)
        rollout._step_observations = None
        rollout._profile_span("capsule_state_restore_ms", started)
        started = time.perf_counter()
        for owner, saved in self.tensorizers.items():
            _restore_tensorizer(rollout.tensorizers[owner], saved, profile_sink=rollout.profile_sink)
        if (self.overflow is None) != (rollout.overflow is None):
            raise ValueError("event overflow state differs")
        if rollout.overflow is not None:
            for name, value in self.overflow.items():
                setattr(rollout.overflow, name, _clone(value))
        torch.set_rng_state(self.torch_rng)
        if rollout.device.type == "cuda":
            torch.cuda.set_rng_state(self.cuda_rng, rollout.device)
        rollout._profile_span("capsule_tensorizer_restore_ms", started)
        rollout.started = True


class EventPolicyRollout:
    """Use the existing event legality, scheduling, execution and reward contracts.

    Both owners decide from FAIR observations. Search can inspect the complete
    native state, but no opponent private state is inserted into model input.
    The residual event core resets its state on every decision, as PPO does.
    """

    def __init__(self, environment, policies: Mapping[int, Any], *, reward,
                 match_id: str, learner_owner: int = 0, temperatures=(1., 1., 1., 1.),
                 max_advance_ticks: int = 120, lightning_constraint=None, log_constraint=None):
        if set(policies) != {0, 1} or learner_owner not in (0, 1):
            raise ValueError("event rollout requires two policies and a learner owner")
        self.environment = environment
        self.policies = dict(policies)
        self.models = {o: p.model.eval() for o, p in policies.items()}
        self.device = next(self.models[0].parameters()).device
        self.config = self.models[0].event_config
        if any(m.event_config != self.config for m in self.models.values()):
            raise ValueError("event policy timing contracts differ")
        if not all(m.backbone.config.snapshot_only for m in self.models.values()):
            raise ValueError("this driver requires the verified stateless event checkpoint")
        self.reward_config = reward_config(reward)
        self.match_id = match_id
        self.learner_owner = learner_owner
        self.temperatures = tuple(temperatures)
        self.max_advance_ticks = int(max_advance_ticks)
        self.lightning_constraint = lightning_constraint
        self.log_constraint = log_constraint
        self.bundle = production_semantic_bundle()
        self.tensorizers = {}
        self.started = False
        self.profile_counters = {}
        self.profile_sink = None
        self.context_cache_enabled = True
        self.context_content_cache_size = 8
        self._decision_context_content_cache = OrderedDict()
        self.search_value_cache_enabled = os.environ.get("PUCT_LEAF_VALUE_CACHE", "1") != "0"
        self._search_value_cache = OrderedDict()
        self.search_policy_cache_enabled = os.environ.get("PUCT_POLICY_PRIOR_CACHE", "1") != "0"
        self._search_policy_cache = OrderedDict()
        self.reuse_step_observations = os.environ.get("PUCT_REUSE_STEP_OBSERVATIONS", "1") != "0"
        self.lazy_step_observations = os.environ.get("PUCT_LAZY_STEP_OBSERVATIONS", "1") != "0"
        self.step_timing_profile = None

    def _count_profile(self, name: str, amount: int = 1) -> None:
        if self.profile_sink is None:
            return
        self.profile_counters[name] = self.profile_counters.get(name, 0) + int(amount)

    def _profile_span(self, name: str, started: float) -> None:
        if self.profile_sink is not None:
            self.profile_sink(name, started)

    def reset(self, *, decks: Mapping[int, tuple[int, ...]], seed: int):
        env = self.environment
        match = MatchConfig(deck0=list(decks[0]), deck1=list(decks[1]), seed=int(seed),
                            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())
        observations, _ = env.reset(match_config=match, options={
            "render_mode": "headless", "decision_ticks": POLICY_DECISION_TICKS,
            "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True,
        })
        initial_elixir = {
            o: next(p.elixir_exact for p in observations[o].players if p.owner == o) for o in (0, 1)
        }
        self.initial_tower_hp = {t.entity_id: t.max_hitpoints for t in observations[0].towers}
        for owner in (0, 1):
            tensorizer = build_episode_tensorizer_v4(env.episode_config, actor_owner=owner)
            tensorizer.start_episode(observations[owner], initial_elixir=initial_elixir)
            self.tensorizers[owner] = tensorizer
        self.recorder = EventLaneRecorderV1(
            match_id=self.match_id, policy_version="hog-tree-search-1755",
            temperatures=self.temperatures, decks=decks,
            behavior_checkpoint_sha256=self.policies[self.learner_owner].checkpoint_sha256,
            behavior_model_contract_hash=self.policies[self.learner_owner].contract_sha256,
            action_constraints=action_constraints_contract(self.lightning_constraint, self.log_constraint),
            reward_config=self.reward_config,
        )
        self.overflow = (ElixirOverflowMeter(env.mode_timeline.timeline, self.reward_config)
                         if self.reward_config is not None else None)
        self.plans = {0: None, 1: None}
        self.ledger = EventExecutionLedger()
        self.recovery_owners = set()
        self.event_ticks = []
        self.seen_events = set()
        self.tick = int(observations[0].tick)
        self.last_decision_by_owner = {0: self.tick, 1: self.tick}
        self.ready, self.frames, self.legals = [], {}, {}
        self.terminal = False
        self.totals = {0: 0., 1: 0.}
        self.decisions = self.steps = self.cancellations = self.native_events = 0
        self._search_recorder_capsule = None
        self._search_execution_ledger = None
        self._search_recorder_lengths = None
        self._root_rollout_capsule = None
        self._root_rollout_capsule_audited = False
        self._decision_context_cache = None
        self._decision_context_content_cache.clear()
        self._search_value_cache.clear()
        self._search_policy_cache.clear()
        self._search_restore_cache = OrderedDict()
        self._step_observations = observations
        self.started = True
        return observations

    def _legality(self, owner, observation, frame):
        model = self.models[owner]
        return legal_event_actions(
            self.environment, observation, frame, owner, catalog=self.bundle.card_catalog,
            card_count=model.card_count, cell_count=model.cell_count,
            board_width=self.config.board_width, board_height=self.config.board_height,
            delay_offset_ms=self.config.delay_offset_ms,
            lightning_constraint=self.lightning_constraint, log_constraint=self.log_constraint,
            device=self.device,
        )

    def context(self, owner, *, profile_span="model.context_encode_ms"):
        frame_object = self.frames[owner]
        cached = getattr(self, "_decision_context_cache", None)
        cache_enabled = getattr(self, "context_cache_enabled", False)
        if cache_enabled and cached is not None and cached[0] == owner and cached[1] is frame_object:
            self._count_profile("model_context_cache_hits")
            return cached[2]
        model = self.models[owner]
        frame = frame_object.to_model_input(self.device, float_dtype=torch.float32)
        content_cache = getattr(self, "_decision_context_content_cache", None)
        if cache_enabled and content_cache is None:
            content_cache = OrderedDict()
            self._decision_context_content_cache = content_cache
        content_key = None
        if cache_enabled:
            digest_started = time.perf_counter()
            try:
                self._count_profile("model_context_digest_calls")
                content_key = (owner, fast_semantic_digest(frame_object))
                cached_context = content_cache.get(content_key)
                if cached_context is not None:
                    content_cache.move_to_end(content_key)
                    self._decision_context_cache = (owner, frame_object, cached_context)
                    self._count_profile("model_context_content_cache_hits")
                    return cached_context
            finally:
                self._profile_span("model.context_digest_ms", digest_started)
        started = time.perf_counter()
        try:
            self._count_profile("model_context_encode_calls")
            context = model.encode(frame, model.initial_state(1, device=self.device),
                                   episode_start=torch.ones(1, dtype=torch.bool, device=self.device))
            if cache_enabled:
                self._decision_context_cache = (owner, frame_object, context)
                content_cache[content_key] = context
                content_cache.move_to_end(content_key)
                while len(content_cache) > getattr(self, "context_content_cache_size", 8):
                    content_cache.popitem(last=False)
            return context
        finally:
            self._profile_span(profile_span, started)

    def current_policy_observations(self):
        """Return the observations produced by the latest native step."""
        observations = getattr(self, "_step_observations", None)
        if observations is None:
            observations = self.environment.observe_dual(policy_features_only=True)
            self._step_observations = observations
        return observations

    def masks(self, owner):
        legal = self.legals[owner]
        return dict(
            gate_mask=torch.tensor([[True, legal.act_allowed]], dtype=torch.bool, device=self.device),
            card_mask=legal.card_mask.unsqueeze(0).to(self.device),
            cell_masks=legal.cell_masks.unsqueeze(0).to(self.device),
            delay_mask=legal.delay_masks.unsqueeze(0).to(self.device),
        )

    def legal_action_keys(self, owner: int) -> tuple[str, ...]:
        """Enumerate the exact event actions represented by the current masks."""

        if not self.ready or self.ready[0] != owner:
            raise ValueError("event action enumeration does not match the active owner")
        legal = self.legals[owner]
        keys = ["wait"]
        if self.models[owner].config.ability_actions:
            candidates = candidate_ability_mask(self.frames[owner]).flatten()
            for source in (int(index) for index in candidates.nonzero(as_tuple=False)):
                delays = (range(len(self.config.delay_offset_ms))
                          if self.models[owner].config.ability_delay else (-1,))
                keys.extend(f"ability:{source}:{delay}" for delay in delays)
        for row in legal.legal_rows:
            cells = tuple(int(index) for index in legal.cell_masks[row].nonzero(as_tuple=False))
            delays = tuple(int(index) for index in legal.delay_masks[row].nonzero(as_tuple=False))
            for cell in cells:
                keys.extend(f"play:{row}:{cell}:{delay}" for delay in delays)
        return tuple(keys)

    def packed_action_space(self, owner: int) -> PackedActionSpace:
        """Build the exact legal action order without creating action strings."""
        if not self.ready or self.ready[0] != owner:
            raise ValueError("packed action enumeration does not match the active owner")
        legal = self.legals[owner]
        kind = [torch.zeros(1, dtype=torch.int8)]
        rows = [torch.full((1,), -1, dtype=torch.long)]
        cells = [torch.full((1,), -1, dtype=torch.long)]
        delays = [torch.full((1,), -1, dtype=torch.long)]
        sources = [torch.full((1,), -1, dtype=torch.long)]
        model = self.models[owner]
        if model.config.ability_actions:
            candidates = candidate_ability_mask(self.frames[owner]).flatten().nonzero(
                as_tuple=False).flatten().to(dtype=torch.long, device="cpu")
            if len(candidates):
                if model.config.ability_delay:
                    delay_values = torch.arange(len(self.config.delay_offset_ms), dtype=torch.long)
                    sources.append(candidates.repeat_interleave(len(delay_values)))
                    delays.append(delay_values.repeat(len(candidates)))
                    kind.append(torch.full((len(candidates) * len(delay_values),), 2, dtype=torch.int8))
                else:
                    sources.append(candidates)
                    delays.append(torch.full((len(candidates),), -1, dtype=torch.long))
                    kind.append(torch.full((len(candidates),), 2, dtype=torch.int8))
                rows.append(torch.full_like(sources[-1], -1))
                cells.append(torch.full_like(sources[-1], -1))
        rows_tensor = torch.tensor(legal.legal_rows, dtype=torch.long)
        if len(rows_tensor):
            row_mask_indices = rows_tensor.to(device=legal.cell_masks.device)
            row_cell_masks = legal.cell_masks.index_select(0, row_mask_indices)
            row_positions, cell_values = row_cell_masks.nonzero(as_tuple=True)
            if len(cell_values):
                row_delay_masks = legal.delay_masks.index_select(0, row_mask_indices)
                cell_delay_masks = row_delay_masks.index_select(0, row_positions)
                cell_positions, delay_values = cell_delay_masks.nonzero(as_tuple=True)
                row_positions_cpu = row_positions.cpu()
                cell_positions_cpu = cell_positions.cpu()
                kind.append(torch.ones(len(delay_values), dtype=torch.int8))
                rows.append(rows_tensor.index_select(0, row_positions_cpu).index_select(0, cell_positions_cpu))
                cells.append(cell_values.index_select(0, cell_positions).cpu())
                delays.append(delay_values.cpu())
                sources.append(torch.full((len(delay_values),), -1, dtype=torch.long))
        fields = tuple(torch.cat(values) for values in (kind, rows, cells, delays, sources))
        return PackedActionSpace(*fields, canonical_tie_rank=torch.arange(len(fields[0]), dtype=torch.long))

    def decode_packed_action(self, owner: int, index: int) -> str:
        if not self.ready or self.ready[0] != owner:
            raise ValueError("packed action decode does not match the active owner")
        index = int(index)
        if index == 0:
            return "wait"
        if index < 0:
            raise IndexError("packed action index is outside the action space")
        model = self.models[owner]
        if model.config.ability_actions:
            sources = candidate_ability_mask(self.frames[owner]).flatten().nonzero(
                as_tuple=False).flatten()
            delay_count = len(self.config.delay_offset_ms) if model.config.ability_delay else 1
            ability_count = len(sources) * delay_count
            if index <= ability_count:
                source_index, delay = divmod(index - 1, delay_count)
                return f"ability:{int(sources[source_index])}:{delay if model.config.ability_delay else -1}"
            index -= ability_count
        legal = self.legals[owner]
        for row in legal.legal_rows:
            cells = legal.cell_masks[row].nonzero(as_tuple=False).flatten()
            delays = legal.delay_masks[row].nonzero(as_tuple=False).flatten()
            count = len(cells) * len(delays)
            if index <= count:
                cell_index, delay_index = divmod(index - 1, len(delays))
                return f"play:{row}:{int(cells[cell_index])}:{int(delays[delay_index])}"
            index -= count
        raise IndexError("packed action index is outside the action space")

    def packed_policy_prior(self, owner: int, *, space: PackedActionSpace | None = None) -> PackedPrior:
        """Return joint prior mass aligned to numeric action identities."""
        if not self.ready or self.ready[0] != owner:
            raise ValueError("packed prior does not match the active owner")
        self._count_profile("policy_prior_calls")
        if space is None:
            space = self.packed_action_space(owner)
        model = self.models[owner]
        cache_enabled = (
            getattr(self, "search_policy_cache_enabled", False)
            and not model.training
            and (getattr(self, "_search_execution_ledger", None) is not None
                 or getattr(self, "_search_recorder_capsule", None) is not None)
        )
        cache_key = None
        if cache_enabled:
            started = time.perf_counter()
            try:
                cache_key = (owner, id(model), fast_semantic_digest(self.frames[owner]),
                             space.semantic_sha256, self.temperatures)
                cached_prior = self._search_policy_cache.get(cache_key)
                if cached_prior is not None:
                    self._search_policy_cache.move_to_end(cache_key)
                    self._count_profile("model_policy_cache_hits")
                    return PackedPrior(space, cached_prior.mass)
                self._count_profile("model_policy_cache_misses")
            finally:
                self._profile_span("model.policy_digest_ms", started)
        legal = self.legals[owner]
        with torch.inference_mode():
            context = self.context(owner, profile_span="prior.context_encode_ms")
            masks = self.masks(owner)
            heads_started = time.perf_counter()
            gate_scores, gate_mask = model.event_gate(context, masks["gate_mask"])
            gate_log = _distribution(gate_scores, mask=gate_mask,
                                      temperature=self.temperatures[0], validate=False).logits[0]
            card_log = _distribution(model.card_logits(context), mask=masks["card_mask"],
                                     temperature=self.temperatures[1], validate=False).logits[0]
            ability_log = (model.ability_distribution(context, self.temperatures[1], validate=False).logits[0]
                           if model.config.ability_actions else None)
            self._profile_span("prior.policy_heads_ms", heads_started)
            log_values = [gate_log[0].reshape(1)]
            if ability_log is not None:
                candidates = candidate_ability_mask(self.frames[owner]).flatten()
                ability_values = []
                for source in (int(index) for index in candidates.nonzero(as_tuple=False)):
                    if model.config.ability_delay:
                        source_tensor = torch.tensor([source], dtype=torch.long, device=self.device)
                        ability_delay = model.action_delay_distribution(
                            context, torch.full((1,), -1, dtype=torch.long, device=self.device),
                            torch.full((1,), -1, dtype=torch.long, device=self.device),
                            source_tensor, masks["delay_mask"], self.temperatures[3], False,
                        ).logits[0]
                        ability_values.append(gate_log[2] + ability_log[source] + ability_delay)
                    else:
                        ability_values.append((gate_log[2] + ability_log[source]).reshape(1))
                if ability_values:
                    log_values.append(torch.cat(ability_values))
            rows_tensor = torch.tensor(legal.legal_rows, dtype=torch.long, device=self.device)
            if len(rows_tensor):
                batched_context = replace(
                    context,
                    policy_context=context.policy_context.expand(len(rows_tensor), -1),
                    encoded=replace(
                        context.encoded,
                        own_card_memory=context.encoded.own_card_memory.expand(len(rows_tensor), -1, -1),
                        spatial_memory=context.encoded.spatial_memory.expand(len(rows_tensor), -1, -1, -1),
                    ),
                )
                conditioning_started = time.perf_counter()
                spatial_keys, cell_query, condition_input = model._cell_conditioning(
                    batched_context, rows_tensor)
                row_indices = rows_tensor.to(device=legal.cell_masks.device)
                row_cell_masks = legal.cell_masks.index_select(0, row_indices).to(self.device)
                cell_log = _distribution(
                    model._score_cells(cell_query, spatial_keys), mask=row_cell_masks,
                    temperature=self.temperatures[2], validate=False,
                ).logits
                self._profile_span("prior.cell_conditioning_ms", conditioning_started)
                row_positions, cells_tensor = row_cell_masks.nonzero(as_tuple=True)
                if len(cells_tensor):
                    spatial_memory = spatial_keys.flatten(2).transpose(1, 2)
                    selected = spatial_memory[row_positions, cells_tensor]
                    condition = condition_input.index_select(0, row_positions)
                    delay_logits = model.delay_head(torch.cat((condition, selected), dim=1))
                    row_delay_masks = legal.delay_masks.index_select(
                        0, rows_tensor.to(device=legal.delay_masks.device)
                    ).to(self.device)
                    delay_log = _distribution(
                        delay_logits, mask=row_delay_masks.index_select(0, row_positions),
                        temperature=self.temperatures[3], validate=False,
                    ).logits
                    play_log = (
                        gate_log[1]
                        + card_log.index_select(0, rows_tensor).index_select(0, row_positions)[:, None]
                        + cell_log[row_positions, cells_tensor, None]
                        + delay_log
                    ).masked_select(row_delay_masks.index_select(0, row_positions))
                    log_values.append(play_log)
            log_values = torch.cat(log_values)
            if len(log_values) != len(space):
                raise ValueError("packed event prior does not cover the legal action space")
            mass = torch.exp(log_values - log_values.max())
        self._count_profile("legal_action_count_sum", len(space))
        self._count_profile("prior_action_materialized_count", len(space))
        self._count_profile("packed_prior_elements", len(space))
        result = PackedPrior(space, mass)
        if cache_enabled:
            self._search_policy_cache[cache_key] = result
            self._search_policy_cache.move_to_end(cache_key)
            while len(self._search_policy_cache) > _SEARCH_POLICY_CACHE_SIZE:
                self._search_policy_cache.popitem(last=False)
        return result

    def policy_prior(self, owner: int) -> dict[str, float]:
        """Return positive joint masses using one backbone pass per decision."""

        self._count_profile("policy_prior_calls")
        keys = self.legal_action_keys(owner)
        model = self.models[owner]
        with torch.inference_mode():
            context = self.context(owner, profile_span="prior.context_encode_ms")
            masks = self.masks(owner)
            heads_started = time.perf_counter()
            gate_scores, gate_mask = model.event_gate(context, masks["gate_mask"])
            gate_log = _distribution(
                gate_scores, mask=gate_mask, temperature=self.temperatures[0], validate=False
            ).logits[0]
            card_log = _distribution(
                model.card_logits(context),
                mask=masks["card_mask"],
                temperature=self.temperatures[1],
                validate=False,
            ).logits[0]
            ability_log = None
            if model.config.ability_actions:
                ability_log = model.ability_distribution(
                    context, self.temperatures[1], validate=False
                ).logits[0]
            self._profile_span("prior.policy_heads_ms", heads_started)

            log_probs = {"wait": float(gate_log[0])}
            legal = self.legals[owner]
            rows = torch.tensor(legal.legal_rows, dtype=torch.long, device=self.device)
            row_count = len(rows)
            batched_context = replace(
                context,
                policy_context=context.policy_context.expand(row_count, -1),
                encoded=replace(
                    context.encoded,
                    own_card_memory=context.encoded.own_card_memory.expand(row_count, -1, -1),
                    spatial_memory=context.encoded.spatial_memory.expand(row_count, -1, -1, -1),
                ),
            )
            conditioning_started = time.perf_counter()
            spatial_keys, cell_query, condition_input = model._cell_conditioning(batched_context, rows)
            row_indices = rows.to(device=legal.cell_masks.device)
            row_cell_masks = legal.cell_masks.index_select(0, row_indices).to(self.device)
            cell_log = _distribution(
                model._score_cells(cell_query, spatial_keys),
                mask=row_cell_masks,
                temperature=self.temperatures[2],
                validate=False,
            ).logits
            self._profile_span("prior.cell_conditioning_ms", conditioning_started)
            row_positions, cells = row_cell_masks.nonzero(as_tuple=True)
            if len(cells):
                spatial_memory = spatial_keys.flatten(2).transpose(1, 2)
                selected = spatial_memory[row_positions, cells]
                condition = condition_input.index_select(0, row_positions)
                delay_logits = model.delay_head(torch.cat((condition, selected), dim=1))
                row_delay_masks = legal.delay_masks.index_select(
                    0, rows.to(device=legal.delay_masks.device)
                ).to(self.device)
                delay_mask = row_delay_masks.index_select(0, row_positions)
                delay_log = _distribution(
                    delay_logits,
                    mask=delay_mask,
                    temperature=self.temperatures[3],
                    validate=False,
                ).logits
                joint_started = time.perf_counter()
                joint_device = (gate_log[1] + card_log.index_select(0, rows)[row_positions, None]
                                + cell_log[row_positions, cells, None] + delay_log)
                self._profile_span("prior.joint_tensor_ms", joint_started)
                sync_started = time.perf_counter()
                joint = joint_device.cpu()
                self._profile_span("prior.device_sync_ms", sync_started)
                self._count_profile("prior_tensor_to_cpu_elements", joint.numel())
                materialize_started = time.perf_counter()
                for index, (row_position, cell) in enumerate(zip(row_positions.tolist(), cells.tolist())):
                    row = int(rows[row_position])
                    delays = row_delay_masks[row_position].nonzero(as_tuple=False).flatten().tolist()
                    for delay in delays:
                        log_probs[f"play:{row}:{cell}:{delay}"] = float(joint[index, delay])
                self._profile_span("prior.python_materialize_ms", materialize_started)
            if ability_log is not None:
                candidates = candidate_ability_mask(self.frames[owner]).flatten()
                for source in (int(index) for index in candidates.nonzero(as_tuple=False)):
                    delays = (range(len(self.config.delay_offset_ms))
                              if model.config.ability_delay else (-1,))
                    for delay in delays:
                        value = gate_log[2] + ability_log[source]
                        if model.config.ability_delay:
                            source_tensor = torch.tensor([source], dtype=torch.long, device=self.device)
                            delay_log = model.action_delay_distribution(
                                context,
                                torch.full((1,), -1, dtype=torch.long, device=self.device),
                                torch.full((1,), -1, dtype=torch.long, device=self.device),
                                source_tensor, masks["delay_mask"],
                                self.temperatures[3], False,
                            ).logits[0]
                            value = value + delay_log[delay]
                        log_probs[f"ability:{source}:{delay}"] = float(value)
            if set(log_probs) != set(keys):
                raise ValueError("joint event prior did not cover the legal action set")
        self._count_profile("legal_action_count_sum", len(keys))
        self._count_profile("prior_action_materialized_count", len(log_probs))
        self._count_profile("canonical_action_string_count", len(keys))
        normalize_started = time.perf_counter()
        maximum = max(log_probs.values())
        result = {key: math.exp(value - maximum) for key, value in log_probs.items()}
        self._profile_span("prior.normalize_ms", normalize_started)
        return result

    def value_estimate(self, owner: int | None = None) -> float:
        """Read the model critic at the active decision, in that actor's view."""

        if owner is None:
            if not self.ready:
                raise ValueError("value estimate requires an active decision")
            owner = self.ready[0]
        if not self.ready or self.ready[0] != owner:
            raise ValueError("value estimate does not match the active owner")
        model = self.models[owner]
        cache_enabled = (
            getattr(self, "search_value_cache_enabled", False)
            and not model.training
            and (getattr(self, "_search_execution_ledger", None) is not None
                 or getattr(self, "_search_recorder_capsule", None) is not None)
        )
        cache_key = None
        if cache_enabled:
            started = time.perf_counter()
            try:
                cache_key = (owner, id(model), fast_semantic_digest(self.frames[owner]))
                cached_value = self._search_value_cache.get(cache_key)
                if cached_value is not None:
                    self._search_value_cache.move_to_end(cache_key)
                    self._count_profile("model_value_cache_hits")
                    return cached_value
                self._count_profile("model_value_cache_misses")
            finally:
                self._profile_span("model.value_digest_ms", started)
        with torch.inference_mode():
            frame = self.frames[owner].to_model_input(self.device, float_dtype=torch.float32)
            started = time.perf_counter()
            self._count_profile("model_value_only_calls")
            try:
                value = model.value_only(
                    frame,
                    model.initial_state(1, device=self.device),
                    episode_start=torch.ones(1, dtype=torch.bool, device=self.device),
                )
            finally:
                self._profile_span("model.context_encode_ms", started)
        if value is None:
            raise ValueError("event model did not return a value estimate")
        result = float(value[0])
        if cache_enabled:
            self._search_value_cache[cache_key] = result
            self._search_value_cache.move_to_end(cache_key)
            while len(self._search_value_cache) > _SEARCH_VALUE_CACHE_SIZE:
                self._search_value_cache.popitem(last=False)
        return result

    def _validate_action(self, owner, action):
        legal = self.legals[owner]
        if not bool(action.act[0]):
            return
        source = int(action.ability_candidate[0])
        if source >= 0:
            from native_runner.training.event_abilities import candidate_ability_mask
            mask = candidate_ability_mask(self.frames[owner])
            if not self.models[owner].config.ability_actions or source >= mask.shape[1] or not mask[0, source]:
                raise ValueError("illegal search ability source")
            delay = int(action.delay_bin[0])
            if self.models[owner].config.ability_delay:
                if not 0 <= delay < len(self.config.delay_offset_ms):
                    raise ValueError("illegal search ability delay")
            elif delay != -1:
                raise ValueError("immediate ability must not carry a delay")
            return
        row, cell, delay = (int(action.card_row[0]), int(action.cell[0]), int(action.delay_bin[0]))
        if (not 0 <= row < legal.card_mask.numel() or not legal.card_mask[row]
                or not 0 <= cell < legal.cell_masks.shape[1] or not legal.cell_masks[row, cell]
                or not 0 <= delay < legal.delay_masks.shape[1] or not legal.delay_masks[row, delay]):
            raise ValueError("illegal search card/cell/delay")

    def decide(self, owner: int, *, key: str | None = None, sample: bool = False):
        if not self.ready or self.ready[0] != owner:
            raise ValueError("event decision does not match the active owner")
        frame, legal = self.frames[owner], self.legals[owner]
        with torch.inference_mode():
            context = self.context(owner, profile_span="apply.context_encode_ms")
            if key is None:
                step = self.models[owner].select_action(
                    context, temperatures=self.temperatures, sample=sample, **self.masks(owner))
            else:
                decode_started = time.perf_counter()
                action = decode_action(key, device=self.device)
                self._validate_action(owner, action)
                self._profile_span("apply.decode_action_ms", decode_started)
                self._count_profile("model_evaluate_action_calls")
                started = time.perf_counter()
                try:
                    step = self.models[owner].evaluate_actions(
                        context, action, temperatures=self.temperatures, **self.masks(owner))
                finally:
                    self._profile_span("apply.evaluate_action_ms", started)
        recorder_started = time.perf_counter()
        self.recorder.decide(
            owner=owner, tick=self.tick, frame=frame, step=step, **self.masks(owner),
            tower_state=tower_state(self.current_policy_observations()[owner].towers,
                                    self.initial_tower_hp),
        )
        self._profile_span("apply.recorder_ms", recorder_started)
        if self.overflow is not None:
            self.overflow.start(owner, self.tick)
        self.plans[owner] = None
        self.ready.pop(0)
        self._decision_context_cache = None
        self.recovery_owners.discard(owner)
        self.last_decision_by_owner[owner] = self.tick
        self.decisions += 1
        self._install_plan(owner, step.action)
        return step

    def commit_search_action(self, owner: int, key: str):
        """Commit a predetermined PUCT action without evaluating or recording it."""
        if self._search_execution_ledger is None:
            raise ValueError("search execution ledger is not active")
        if not self.ready or self.ready[0] != owner:
            raise ValueError("search decision does not match the active owner")
        action = decode_action(key, device=self.device)
        self._validate_action(owner, action)
        if action_key(action) != key:
            raise ValueError("search action identity is not canonical")
        if self.overflow is not None:
            self.overflow.start(owner, self.tick)
        self.plans[owner] = None
        self.ready.pop(0)
        self._decision_context_cache = None
        self.recovery_owners.discard(owner)
        self.last_decision_by_owner[owner] = self.tick
        self.decisions += 1
        scheduled_tick = self._install_plan(owner, action)
        self._search_execution_ledger.commit(owner, self.tick, key, scheduled_tick)

    def _install_plan(self, owner: int, action: EventActionV1):
        if not bool(action.act[0]):
            return None
        frame, legal = self.frames[owner], self.legals[owner]
        plan_started = time.perf_counter()
        if int(action.ability_candidate[0]) >= 0:
            self.plans[owner] = ability_plan(
                frame, int(action.ability_candidate[0]), owner=owner, tick=self.tick,
                ability_catalog=self.bundle.ability_catalog,
                delay_ms=(self.config.bin_to_delay_ms(int(action.delay_bin[0]))
                          if self.models[owner].config.ability_delay else 0))
        else:
            row, cell, delay = (int(action.card_row[0]), int(action.cell[0]),
                                self.config.bin_to_delay_ms(int(action.delay_bin[0])))
            self.plans[owner] = dict(
                card_id=int(legal.row_card_id[row]), hand_index=int(legal.hand_index[row]),
                cell=event_cell_to_native(cell, owner=owner, board_width=self.config.board_width),
                delay_ms=delay, decided_tick=self.tick,
                scheduled_tick=self.tick + max(1, math.ceil(delay / 50)),
            )
        self._profile_span("apply.plan_build_ms", plan_started)
        self.event_ticks.append(self.plans[owner]["scheduled_tick"] + 1)
        return int(self.plans[owner]["scheduled_tick"])

    def _cancel(self, owner, reason):
        if self._search_execution_ledger is not None:
            cancelled = self._search_execution_ledger.cancel(owner, reason)
            self.cancellations += int(cancelled)
            return
        pending = self.recorder._pending[owner]
        cancelled = pending is not None and pending.execution_status == "pending"
        self.recorder.cancel(owner, reason=reason)
        self.cancellations += int(cancelled)

    def _due_commands(self, observations):
        commands = {}
        for owner in (0, 1):
            plan = self.plans[owner]
            if plan is None or self.tick < plan["scheduled_tick"]:
                continue
            self.plans[owner] = None
            self.recovery_owners.add(owner)
            mask = self.environment.action_mask(owner)
            if not mask_decision_ready(mask):
                self._cancel(owner, "decision_window_closed")
                continue
            if observations is None:
                observations = self.current_policy_observations()
            if plan.get("kind") == "activate_ability":
                try:
                    action = ability_action_from_plan(
                        self.environment, observations[owner], owner, plan,
                        action_id=f"{self.match_id}:{self.ledger.submitted}:{owner}")
                except ValueError:
                    self._cancel(owner, "ability_no_longer_legal")
                    continue
            else:
                player = next(p for p in observations[owner].players if p.owner == owner)
                slots = {card: slot for slot, card in native_hand_slots(player)}
                hand = slots.get(plan["card_id"])
                if hand is None:
                    self._cancel(owner, "card_left_hand")
                    continue
                if hand >= len(mask.hand_slots) or not mask.hand_slots[hand]:
                    self._cancel(owner, "hand_slot")
                    continue
                if not mask.kinds.get("play_card", False):
                    self._cancel(owner, "play_kind")
                    continue
                if not _cell_is_legal(mask, hand, plan["cell"]):
                    self._cancel(owner, "illegal_cell")
                    continue
                if not spell_target_allowed(observations[owner], owner, plan["card_id"], plan["cell"],
                                            lightning_constraint=self.lightning_constraint,
                                            log_constraint=self.log_constraint):
                    self._cancel(owner, "spell_target")
                    continue
                action = ActionV1.play(owner, hand, plan["cell"], card_id=plan["card_id"],
                                       execute_offset_ticks=1,
                                       action_id=f"{self.match_id}:{self.ledger.submitted}:{owner}")
            commands[owner] = (action,)
            self.ledger.submit(action)
            if self._search_execution_ledger is None:
                self.recorder.submit(owner, tick=plan["decided_tick"], action_id=action.action_id)
            else:
                self._search_execution_ledger.submit(owner, plan["decided_tick"], action)
            self.recovery_owners.discard(owner)
        return commands

    def step_native(self, *, stop_tick: int | None = None):
        self._decision_context_cache = None
        if self.terminal or self.ready:
            raise ValueError("native advance requires a live state without pending decisions")
        if stop_tick is not None and stop_tick <= self.tick:
            raise ValueError("search stop tick must be later than the current tick")
        env = self.environment
        observations = None
        if not getattr(self, "lazy_step_observations", True):
            observations = (self._step_observations
                            if getattr(self, "reuse_step_observations", True) else None)
            if observations is None:
                observations = env.observe_dual(policy_features_only=True)
        gameplay_open = policy_actions_open(env, self.tick)
        if not gameplay_open:
            for owner in (0, 1):
                self._cancel(owner, "gameplay_end")
                self.plans[owner] = None
            self.recovery_owners.clear()
        commands = self._due_commands(observations)
        if not commands:
            self.event_ticks = [when for when in self.event_ticks if when > self.tick]
            span = wait_span(self.tick, self.plans.values(), self.event_ticks,
                             min(self.last_decision_by_owner.values()),
                             self.max_advance_ticks, self.config.max_wait_ticks)
            if not gameplay_open:
                span = self.max_advance_ticks
            elif self.recovery_owners:
                span = 1
            if gameplay_open:
                span = min(span, max(1, gameplay_end_tick(env) - self.tick))
            if stop_tick is not None:
                span = min(span, max(1, stop_tick - self.tick))
            commands = {o: (ActionV1.wait(o, ticks=span),) for o in (0, 1)}
        before = raw_elixir(env.raw_observation) if self.overflow is not None else None
        old_tick = self.tick
        step_observations, rewards, terms, truncs, _ = env.step(
            complete_event_commands(commands), record_trace=False, policy_features_only=True,
            defer_observations=True, timing_profile=self.step_timing_profile)
        self._step_observations = None
        current_tick = getattr(env, "current_tick", None)
        if current_tick is None:
            if step_observations:
                current_tick = step_observations[0].tick
            else:
                current_tick = env.raw_observation["tick"]
        self.tick = int(current_tick)
        if stop_tick is not None and self.tick > stop_tick:
            raise ValueError("native advance exceeded requested search horizon")
        self.steps += 1
        self.terminal = bool(terms["__all__"] or truncs["__all__"])
        outcomes = tuple(env.action_outcomes())
        self.recovery_owners.update(self.ledger.reconcile(outcomes))
        self._count_profile("search_execution_reconcile_calls")
        if self._search_execution_ledger is None:
            self.recorder.reconcile(outcomes)
        else:
            self._search_execution_ledger.reconcile(outcomes)
        penalties = None if self.overflow is None else self.overflow.advance(
            start=old_tick, stop=self.tick, before=before, after=raw_elixir(env.raw_observation),
            outcomes=outcomes, gameplay_before=gameplay_open,
            gameplay_after=policy_actions_open(env, self.tick))
        self._count_profile("search_execution_advance_calls")
        if self._search_execution_ledger is None:
            self.recorder.advance(
                ticks=self.tick - old_tick, rewards=rewards, overflow_penalties=penalties,
                done=bool(terms["__all__"]), truncated=bool(truncs["__all__"]))
        elif self.terminal:
            self._search_execution_ledger.close_episode()
        for owner in (0, 1):
            self.totals[owner] += float(rewards[owner]) - (penalties or {}).get(owner, 0.)
        fresh = list(self.ledger.ability_events)
        for owner in (0, 1):
            for item in env.deployment_timing_events(owner):
                event_id = str(item.get("nativeEventId", ""))
                if not event_id or event_id in self.seen_events or item.get("kind") != "card_play":
                    continue
                self.seen_events.add(event_id)
                fresh.append(item)
        self.native_events += len(fresh)
        if self.terminal or not policy_actions_open(env, self.tick):
            return outcomes
        owners = ({0, 1} if fresh else
                  {o for o in (0, 1) if self.tick - self.last_decision_by_owner[o] >= self.config.max_wait_ticks}
                  | self.recovery_owners)
        if owners:
            observations = env.observe_dual(policy_features_only=True)
            self._step_observations = observations
        elif step_observations:
            self._step_observations = step_observations
            observations = step_observations
        for owner in (self.learner_owner, 1 - self.learner_owner):
            if owner not in owners:
                continue
            frame = canonicalize_event_observation(
                self.tensorizers[owner].tensorize(observations[owner], validate=False))
            legal = self._legality(owner, observations[owner], frame)
            if not legal.decision_ready:
                continue
            self.frames[owner], self.legals[owner] = frame, legal
            self.ready.append(owner)
        return outcomes

    def capture(self):
        if not self.started:
            raise ValueError("event rollout has not started")
        state = dict(
            schema=SCHEMA,
            state={name: getattr(self, name) for name in _STATE_FIELDS},
            tensorizers={o: _tensorizer_snapshot(t) for o, t in self.tensorizers.items()},
            overflow=(None if self.overflow is None else
                      {name: getattr(self.overflow, name) for name in ("started", "receipts", "owners")}),
            torch_rng=torch.get_rng_state(),
            cuda_rng=(torch.cuda.get_rng_state(self.device) if self.device.type == "cuda" else None),
        )
        return _encode(state)

    def _remember_search_snapshot(self, state, decoded):
        cache = getattr(self, "_search_restore_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._search_restore_cache = cache
        cache[id(state)] = (state, decoded)
        cache.move_to_end(id(state))
        while len(cache) > _SEARCH_RESTORE_CACHE_SIZE:
            cache.popitem(last=False)

    def _cached_search_snapshot(self, state):
        cache = getattr(self, "_search_restore_cache", None)
        if cache is None:
            return None
        item = cache.get(id(state))
        if item is None or item[0] is not state:
            return None
        cache.move_to_end(id(state))
        return item[1]

    def capture_search(self):
        if not self.started:
            raise ValueError("event rollout has not started")
        decoded_state = {}
        if self._search_execution_ledger is not None:
            state = {}
            for name in _STATE_FIELDS:
                if name == "recorder":
                    value = self._search_execution_ledger.to_state()
                    state[name] = value
                else:
                    value = _clone(getattr(self, name))
                    decoded_state[name] = value
                    state[name] = _encode(value)
            tensorizers = {o: _tensorizer_snapshot(t) for o, t in self.tensorizers.items()}
            overflow = (None if self.overflow is None else
                        {name: _clone(getattr(self.overflow, name))
                         for name in ("started", "receipts", "owners")})
            torch_rng = torch.get_rng_state().clone()
            cuda_rng = (torch.cuda.get_rng_state(self.device).clone()
                        if self.device.type == "cuda" else None)
            result = {
                "schema": SCHEMA,
                "state": state,
                "tensorizers": _encode(tensorizers),
                "overflow": _encode(overflow),
                "torch_rng": _encode(torch_rng),
                "cuda_rng": _encode(cuda_rng),
            }
            decoded_state["recorder"] = state["recorder"]
            self._remember_search_snapshot(
                result, (decoded_state, tensorizers, overflow, torch_rng, cuda_rng))
            return result
        if self._search_recorder_capsule is None:
            self._search_recorder_capsule = RootRecorderCapsule.from_recorder(self.recorder)
        delta = RecorderBranchDelta.from_recorder(self.recorder, self._search_recorder_capsule)
        state = {}
        for name in _STATE_FIELDS:
            if name == "recorder":
                value = SearchRecorderState(self._search_recorder_capsule, delta)
                state[name] = value
            else:
                value = _clone(getattr(self, name))
                decoded_state[name] = value
                state[name] = _encode(value)
        decoded_state["recorder"] = state["recorder"]
        tensorizers = {o: _tensorizer_snapshot(t) for o, t in self.tensorizers.items()}
        overflow = (None if self.overflow is None else
                    {name: _clone(getattr(self.overflow, name))
                     for name in ("started", "receipts", "owners")})
        torch_rng = torch.get_rng_state().clone()
        cuda_rng = (torch.cuda.get_rng_state(self.device).clone()
                    if self.device.type == "cuda" else None)
        result = {
            "schema": SCHEMA,
            "state": state,
            "tensorizers": _encode(tensorizers),
            "overflow": _encode(overflow),
            "torch_rng": _encode(torch_rng),
            "cuda_rng": _encode(cuda_rng),
        }
        self._remember_search_snapshot(
            result, (decoded_state, tensorizers, overflow, torch_rng, cuda_rng))
        return result

    def capture_search_checkpoint(self) -> RootRolloutCapsule:
        """Capture a native-edge checkpoint without JSON encoding."""
        return RootRolloutCapsule.from_rollout(self, audit_digest=False)

    def restore_search_checkpoint(self, capsule: RootRolloutCapsule, receipt: Any) -> None:
        """Restore an edge capsule after the matching native snapshot is live."""
        if not isinstance(capsule, RootRolloutCapsule):
            raise TypeError("search checkpoint capsule type differs")
        capsule.restore_into(self, state_epoch=receipt.state_epoch, verify_digest=False)
        self._decision_context_cache = None

    def begin_search_root(self, *, search_only: bool = False) -> None:
        """Freeze the recorder prefix for one root replay lifetime."""
        if not self.started:
            raise ValueError("event rollout has not started")
        if (getattr(self, "_search_execution_ledger", None) is not None
                or getattr(self, "_search_recorder_capsule", None) is not None):
            raise ValueError("search root is already active")
        self._search_restore_cache = OrderedDict()
        if not hasattr(self, "_search_execution_ledger"):
            self._search_execution_ledger = None
        self._root_rollout_capsule = None
        self._root_rollout_capsule_audited = False
        if search_only:
            self._search_recorder_lengths = self._recorder_lengths()
            self._search_execution_ledger = SearchExecutionLedger.from_recorder(self.recorder)
        else:
            self._search_recorder_capsule = RootRecorderCapsule.from_recorder(self.recorder)
            self._search_recorder_lengths = None
        self.profile_counters = {}
        self._search_value_cache = OrderedDict()
        self._search_policy_cache = OrderedDict()

    def end_search_root(self) -> None:
        if self._search_recorder_capsule is None and self._search_execution_ledger is None:
            raise ValueError("search root is not active")
        if self._root_rollout_capsule is not None:
            self._root_rollout_capsule.audit()
        self._search_execution_ledger = None
        self._search_recorder_capsule = None
        self._search_recorder_lengths = None
        self._root_rollout_capsule = None
        self._search_restore_cache = OrderedDict()
        self._root_rollout_capsule_audited = False
        self._search_value_cache = OrderedDict()
        self._search_policy_cache = OrderedDict()

    def capture_root_capsule(self) -> RootRolloutCapsule:
        """Capture the P13 root capsule once per search root."""
        if self._root_rollout_capsule is not None:
            raise ValueError("root rollout capsule is already captured")
        self._root_rollout_capsule = RootRolloutCapsule.from_rollout(self)
        return self._root_rollout_capsule

    def restore_root_capsule(self, capsule: RootRolloutCapsule, receipt: Any) -> None:
        """Restore search-only policy state after native root restore."""
        if not isinstance(capsule, RootRolloutCapsule):
            raise TypeError("P13 restore requires a RootRolloutCapsule")
        if capsule is not self._root_rollout_capsule:
            raise ValueError("P13 root capsule does not belong to this search root")
        if not self._root_rollout_capsule_audited:
            capsule.audit()
            self._root_rollout_capsule_audited = True
        capsule.restore_into(self, state_epoch=receipt.state_epoch, verify_digest=False)
        self._decision_context_cache = None

    def _rebind_seen_event_epoch(self, state_epoch: int | None = None):
        raw = None
        if state_epoch is None:
            observe = getattr(self.environment.native, "observe", None)
            if callable(observe):
                candidate = observe()
                if isinstance(candidate, Mapping):
                    raw = candidate
            if raw is None:
                candidate = getattr(self.environment, "raw_observation", None)
                if isinstance(candidate, Mapping):
                    raw = candidate
        epoch = state_epoch if state_epoch is not None else (None if raw is None else raw.get("stateEpoch"))
        if epoch is None:
            return
        rebound = set()
        for identity in self.seen_events:
            if not isinstance(identity, str):
                raise ValueError("event identity is malformed")
            parts = identity.split(":")
            if len(parts) == 2:
                _, sequence = parts
                rebound.add(f"{int(epoch)}:{int(sequence)}")
            elif len(parts) >= 3:
                scope = ":".join(parts[:-2])
                _, sequence = parts[-2:]
                rebound.add(f"{scope}:{int(epoch)}:{int(sequence)}")
            else:
                raise ValueError("event identity is malformed")
        self.seen_events = rebound

    def restore(self, encoded):
        self._decision_context_cache = None
        self._step_observations = None
        state = _decode(encoded)
        if state.get("schema") != SCHEMA or set(state["state"]) != set(_STATE_FIELDS):
            raise ValueError("event rollout restore schema differs")
        for name, value in state["state"].items():
            setattr(self, name, value)
        self._rebind_seen_event_epoch()
        for owner, saved in state["tensorizers"].items():
            _restore_tensorizer(self.tensorizers[owner], saved)
        if (state["overflow"] is None) != (self.overflow is None):
            raise ValueError("event overflow state differs")
        if self.overflow is not None:
            for name, value in state["overflow"].items():
                setattr(self.overflow, name, value)
        # JSON preserves values but not shared object references. Relink each
        # in-flight receipt to the open row that must receive its execution credit.
        for owner in (0, 1):
            pending = self.recorder._pending[owner]
            if pending is not None and pending.action_id in self.recorder._submitted:
                self.recorder._submitted[pending.action_id] = pending
        for identity, pending in self.recorder._submitted.items():
            if identity != pending.action_id or pending.owner not in (0, 1):
                raise ValueError("restored receipt identity differs from its decision")
            if pending.closed_index is not None:
                rows = self.recorder.rows[pending.owner]
                if (not 0 <= pending.closed_index < len(rows)
                        or rows[pending.closed_index].action_id != identity):
                    raise ValueError("restored closed receipt has no matching audit row")
        torch.set_rng_state(state["torch_rng"])
        if self.device.type == "cuda":
            torch.cuda.set_rng_state(state["cuda_rng"], self.device)
        self.started = True

    def restore_search(self, state):
        self._decision_context_cache = None
        self._step_observations = None
        if state.get("schema") != SCHEMA or set(state["state"]) != set(_STATE_FIELDS):
            raise ValueError("event rollout restore schema differs")
        cached = self._cached_search_snapshot(state)
        if cached is None:
            decoded_state = {}
            for name, value in state["state"].items():
                decoded_state[name] = value if name == "recorder" else _decode(value)
            tensorizers = _decode(state["tensorizers"])
            overflow = _decode(state["overflow"])
            torch_rng = _decode(state["torch_rng"])
            cuda_rng = _decode(state["cuda_rng"])
            if getattr(self, "profile_sink", None) is not None:
                self._count_profile("search_restore_cache_misses")
        else:
            decoded_state, tensorizers, overflow, torch_rng, cuda_rng = cached
            if getattr(self, "profile_sink", None) is not None:
                self._count_profile("search_restore_cache_hits")
        for name, value in decoded_state.items():
            if name == "recorder":
                if isinstance(value, Mapping) and value.get("schema") == "search-execution-ledger.v1":
                    self._search_execution_ledger = SearchExecutionLedger.from_state(value)
                elif isinstance(value, SearchRecorderState):
                    restore_recorder(self.recorder, value.capsule, value.delta)
                    self._search_recorder_capsule = value.capsule
                    self._search_execution_ledger = None
                else:
                    raise ValueError("search execution state type differs")
            else:
                setattr(self, name, _clone(value))
        self._rebind_seen_event_epoch()
        for owner, saved in tensorizers.items():
            _restore_tensorizer(self.tensorizers[owner], saved)
        if (overflow is None) != (self.overflow is None):
            raise ValueError("event overflow state differs")
        if self.overflow is not None:
            for name, value in overflow.items():
                setattr(self.overflow, name, _clone(value))
        if self._search_execution_ledger is not None:
            torch.set_rng_state(_clone(torch_rng))
            if self.device.type == "cuda":
                torch.cuda.set_rng_state(_clone(cuda_rng), self.device)
            self.started = True
            return
        for owner in (0, 1):
            pending = self.recorder._pending[owner]
            if pending is not None and pending.action_id in self.recorder._submitted:
                self.recorder._submitted[pending.action_id] = pending
        for identity, pending in self.recorder._submitted.items():
            if identity != pending.action_id or pending.owner not in (0, 1):
                raise ValueError("restored receipt identity differs from its decision")
            if pending.closed_index is not None:
                rows = self.recorder.rows[pending.owner]
                if (not 0 <= pending.closed_index < len(rows)
                        or rows[pending.closed_index].action_id != identity):
                    raise ValueError("restored closed receipt has no matching audit row")
        torch.set_rng_state(_clone(torch_rng))
        if self.device.type == "cuda":
            torch.cuda.set_rng_state(_clone(cuda_rng), self.device)
        self.started = True

    def event_cursor(self):
        env = self.environment
        return dict(
            seen=sorted(self.seen_events),
            combat=env._combat_next_sequence,
            public_card_play=env._public_card_play_next_sequence,
            native={
                "combat": (env._combat_event_identity, env._combat_next_sequence),
                "public_card_play": (env._public_card_play_event_identity,
                                      env._public_card_play_next_sequence),
                "phase": (env._phase_event_identity, env._phase_next_sequence),
                "visibility": (env._visibility_event_identity, env._visibility_next_sequence),
                "remaining": (env._remaining_event_identity, env._remaining_next_sequence),
            },
        )

    def reward_ledger(self):
        return dict(totals=deepcopy(self.totals),
                    overflow=None if self.overflow is None else self.overflow.report(),
                    recorder=_encode(self.recorder), execution=_encode(self.ledger))

    def search_reward_ledger(self):
        if self._search_execution_ledger is not None:
            return dict(
                totals=deepcopy(self.totals),
                overflow=None if self.overflow is None else self.overflow.report(),
                execution_digest=semantic_digest(self._search_execution_ledger),
            )
        capsule = self._search_recorder_capsule
        if capsule is None:
            raise ValueError("search recorder capsule is not initialized")
        delta = RecorderBranchDelta.from_recorder(self.recorder, capsule)
        return dict(totals=deepcopy(self.totals),
                    overflow=None if self.overflow is None else self.overflow.report(),
                    recorder={"prefix_digest": capsule.prefix_digest,
                              "branch_digest": delta.digest(capsule)},
                    execution_digest=semantic_digest(self.ledger))

    def search_view_digest(self):
        state = {name: getattr(self, name) for name in _STATE_FIELDS
                 if name not in {"recorder", "seen_events"}}
        overflow = (None if self.overflow is None else
                    {name: getattr(self.overflow, name)
                     for name in ("started", "receipts", "owners")})
        tensorizers = {}
        for owner, tensorizer in self.tensorizers.items():
            tracker = getattr(tensorizer, "tracker", None)
            tensorizers[owner] = {
                "episode": {name: getattr(tensorizer, name) for name in _EPISODE_FIELDS},
                "tracker": (None if tracker is None else {
                    name: getattr(tracker, name) for name in _TRACKER_FIELDS
                }),
            }
        return fast_semantic_digest({
            "state": state,
            "tensorizers": tensorizers,
            "overflow": overflow,
            "torch_rng": torch.get_rng_state(),
            "cuda_rng": torch.cuda.get_rng_state(self.device)
            if self.device.type == "cuda" else None,
        })

    def _recorder_lengths(self):
        return (sum(map(len, self.recorder.rows.values())),
                sum(map(len, self.recorder._frames.values())),
                sum(map(len, self.recorder._row_masks.values())))

    def audit_search_root(self):
        if self._search_execution_ledger is not None:
            if self._search_recorder_lengths != self._recorder_lengths():
                raise ValueError("search branch modified training recorder rows")
            return
        if self._search_recorder_capsule is None:
            raise ValueError("search recorder capsule is not initialized")
        self._search_recorder_capsule.audit()
        expected = self._search_recorder_capsule.logical_lengths
        actual = self._recorder_lengths()
        if actual != expected:
            raise ValueError("search branch modified training recorder rows")
