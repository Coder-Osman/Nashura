"""Task-local native snapshot owner for search branches.

The rollout driver supplies pure capture/restore/action callbacks. Native
snapshot handles stay process-local and are released with the search tree.
"""
from __future__ import annotations

from copy import copy, deepcopy
from collections import deque
from dataclasses import dataclass, replace
import hashlib
import json
import time
from typing import Any, Callable, Mapping

from collections.abc import Mapping as ABCMapping, MutableMapping
from operator import index as _index

from tree_search import SearchState, SearchView, Transition, state_digest
from search_profile import SearchProfile
from native_runner.contracts import EventV1


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class _CopyOnWriteDict(MutableMapping):
    """Mapping view that copies its root data only on the first mutation."""

    __slots__ = ("_source", "_owned")

    def __init__(self, source: Mapping[Any, Any]):
        if not isinstance(source, ABCMapping):
            raise TypeError("copy-on-write source must be a mapping")
        self._source = source
        self._owned: dict[Any, Any] | None = None

    def _view(self) -> Mapping[Any, Any]:
        return self._source if self._owned is None else self._owned

    def _ensure_owned(self) -> dict[Any, Any]:
        if self._owned is None:
            self._owned = dict(self._source)
        return self._owned

    def __getitem__(self, key: Any) -> Any:
        return self._view()[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._ensure_owned()[key] = value

    def __delitem__(self, key: Any) -> None:
        del self._ensure_owned()[key]

    def __iter__(self):
        return iter(self._view())

    def __len__(self) -> int:
        return len(self._view())

    def copy(self) -> dict[Any, Any]:
        return dict(self._view())

    def __repr__(self) -> str:
        return repr(dict(self._view()))


class _EventRingSnapshot:
    """Immutable event-ring view shared by a captured search root."""

    __slots__ = ("_entries", "_maxlen")

    def __init__(self, entries: tuple[EventV1, ...], maxlen: int | None):
        self._entries = entries
        self._maxlen = maxlen

    @property
    def maxlen(self) -> int | None:
        return self._maxlen

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, item: Any) -> EventV1 | tuple[EventV1, ...]:
        return self._entries[item]

    def __iter__(self):
        return iter(self._entries)

    def __repr__(self) -> str:
        return repr(self._entries)


class _CopyOnWriteEventRing:
    """Deque-compatible event ring that materializes only after mutation."""

    __slots__ = ("_base", "_start", "_delta", "_owned", "_maxlen")

    def __init__(self, snapshot: _EventRingSnapshot):
        self._base = snapshot._entries
        self._start = 0
        self._delta: list[EventV1] = []
        self._owned: deque[EventV1] | None = None
        self._maxlen = snapshot.maxlen

    @classmethod
    def from_snapshot(cls, snapshot: _EventRingSnapshot) -> "_CopyOnWriteEventRing":
        return cls(snapshot)

    @property
    def maxlen(self) -> int | None:
        return self._maxlen

    def __len__(self) -> int:
        if self._owned is not None:
            return len(self._owned)
        return len(self._base) - self._start + len(self._delta)

    def __getitem__(self, item: Any) -> EventV1 | tuple[EventV1, ...]:
        if isinstance(item, slice):
            return tuple(self)[item]
        position = _index(item)
        size = len(self)
        if position < 0:
            position += size
        if position < 0 or position >= size:
            raise IndexError("event ring index out of range")
        if self._owned is not None:
            return self._owned[position]
        base_size = len(self._base) - self._start
        if position < base_size:
            return self._base[self._start + position]
        return self._delta[position - base_size]

    def __iter__(self):
        if self._owned is not None:
            yield from self._owned
            return
        yield from self._base[self._start:]
        yield from self._delta

    def append(self, event: EventV1) -> None:
        if self._owned is not None:
            self._owned.append(event)
            return
        if self._maxlen == 0:
            return
        if self._maxlen is None or len(self) < self._maxlen:
            self._delta.append(event)
            return
        if self._start < len(self._base):
            self._start += 1
            self._delta.append(event)
            return
        self._owned = deque(self._delta, maxlen=self._maxlen)
        self._owned.append(event)
        self._delta = []

    def clear(self) -> None:
        self._owned = deque(maxlen=self._maxlen)
        self._delta = []

    def snapshot(self) -> _EventRingSnapshot:
        return _EventRingSnapshot(tuple(self), self._maxlen)

    def __repr__(self) -> str:
        return repr(tuple(self))


class BattleEnvSidecar:
    """Restore all BattleEnv-owned mutable state, including event/reward cursors.

    Static catalogs and the native transport retain their identity. An added
    BattleEnv field is captured by default, so it cannot silently disappear
    from a branch restore.
    """

    STATIC = frozenset({
        "native", "mode_timeline", "card_catalog", "effect_catalog", "card_specs",
        "ability_specs", "semantic_subset_contract", "_semantic_subset_binding",
        "_policy_form_masks", "_explicit_hero_form_base_ids", "ruleset_manifest",
        "ruleset_id", "environment_config", "warmup_ticks", "max_battle_ticks",
        "entity_limit", "event_limit", "event_window_ticks", "shaping_beta",
        "shaping_tau_ticks", "include_native_digest", "princess_only_potential",
        "truncation_terminal_potential", "terminal_margin_beta", "_card_specs_hash",
    })
    SHALLOW_MAPPING_FIELDS = frozenset({
        "_action_mask_cache", "_causal_group_by_entity", "_combat_evolution_entities",
        "_combat_event_kind_by_sequence", "_combat_native_entity_ids", "_entity_birth",
        "_entity_velocity", "_ordinary_tower_cache", "_previous_player_runtime",
        "_previous_rich_objects", "_previous_towers", "_runtime_placement_cache",
        "_tower_object_keys", "_decision_deadline_native", "_last_rewards", "_last_potential",
        "_placement_mask_cache",
    })
    SHALLOW_VALUE_FIELDS = frozenset({"_fair_context_cache", "_fair_scene_cache"})

    def __init__(self, environment: Any, *, discard_epoch_cache: bool = False,
                 share_immutable_events: bool = False,
                 copy_on_write_mappings: bool = False):
        self.environment = environment
        self.profile_sink = None
        self.discard_epoch_cache = discard_epoch_cache
        self.share_immutable_events = share_immutable_events
        self.copy_on_write_mappings = copy_on_write_mappings
        self.keys = frozenset(vars(environment)) - self.STATIC
        if not {"_raw", "_events", "_pending", "_last_rewards", "_last_potential"} <= self.keys:
            raise ValueError("BattleEnv mutable-state contract is incomplete")

    def _copy_field(self, name: str, value: Any, memo: dict[int, Any], *,
                    copy_on_write: bool = False, restoring: bool = False) -> Any:
        if self.share_immutable_events and name == "_events":
            if isinstance(value, _EventRingSnapshot):
                snapshot = value
            elif isinstance(value, _CopyOnWriteEventRing):
                snapshot = value.snapshot()
            elif isinstance(value, deque):
                snapshot = _EventRingSnapshot(tuple(value), value.maxlen)
            else:
                raise ValueError("search event ring must contain immutable EventV1 entries")
            if any(type(item) is not EventV1 for item in snapshot):
                raise ValueError("search event ring must contain immutable EventV1 entries")
            result = (_CopyOnWriteEventRing.from_snapshot(snapshot)
                      if restoring else snapshot)
            memo[id(value)] = result
            for item in snapshot:
                memo[id(item)] = item
            return result
        if name in self.SHALLOW_VALUE_FIELDS:
            memo[id(value)] = value
            return value
        if name in self.SHALLOW_MAPPING_FIELDS and isinstance(value, ABCMapping):
            result = (_CopyOnWriteDict(value) if copy_on_write else dict(value))
            memo[id(value)] = result
            return result
        if name == "_revealed" and isinstance(value, dict):
            result = {owner: set(cards) for owner, cards in value.items()}
            memo[id(value)] = result
            return result
        if name == "_phase_events_by_entity" and isinstance(value, dict):
            result = {key: list(events) for key, events in value.items()}
            memo[id(value)] = result
            return result
        if name in {"_pending", "_pending_abilities"} and isinstance(value, list):
            result = [copy(item) for item in value]
            memo[id(value)] = result
            return result
        if name in {"_step_action_outcomes", "_trace"} and isinstance(value, list):
            result = list(value)
            memo[id(value)] = result
            return result
        if name == "_event_archive" and isinstance(value, dict):
            result = {key: record.copy() for key, record in value.items()}
            memo[id(value)] = result
            return result
        return deepcopy(value, memo)

    def capture(self) -> dict[str, Any]:
        if frozenset(vars(self.environment)) - self.STATIC != self.keys:
            raise ValueError("BattleEnv mutable-state fields changed during search")
        if self.profile_sink is not None:
            memo = {}
            saved = {}
            for name in sorted(self.keys):
                started = time.perf_counter()
                value = ({} if self.discard_epoch_cache and name in {
                    "_action_player_runtime_cache", "_rich_cache"
                } else vars(self.environment)[name])
                saved[name] = self._copy_field(name, value, memo)
                self.profile_sink(f"battle_capture_{name}_ms", started)
            return saved
        if self.discard_epoch_cache or self.share_immutable_events:
            memo = {}
            return {name: self._copy_field(
                name, {} if self.discard_epoch_cache and name in {
                    "_action_player_runtime_cache", "_rich_cache"
                } else vars(self.environment)[name], memo,
            ) for name in sorted(self.keys)}
        return deepcopy({name: vars(self.environment)[name] for name in self.keys})

    def restore(self, saved: Mapping[str, Any], *, receipt: "NativeRestoreReceipt | None" = None) -> None:
        if set(saved) != self.keys or frozenset(vars(self.environment)) - self.STATIC != self.keys:
            raise ValueError("BattleEnv sidecar fields do not match this runtime")
        if self.discard_epoch_cache:
            for name in ("_action_player_runtime_cache", "_rich_cache"):
                if name in self.keys and saved.get(name):
                    raise ValueError("root sidecar retained an epoch-bound runtime cache")
        if self.profile_sink is not None:
            memo = {}
            restored = {}
            for name in sorted(self.keys):
                started = time.perf_counter()
                restored[name] = self._copy_field(
                    name, saved[name], memo,
                    copy_on_write=self.copy_on_write_mappings and self.share_immutable_events,
                    restoring=True,
                )
                self.profile_sink(f"battle_restore_{name}_ms", started)
            vars(self.environment).update(restored)
        else:
            if self.share_immutable_events:
                memo = {}
                vars(self.environment).update({
                    name: self._copy_field(
                        name, saved[name], memo,
                        copy_on_write=self.copy_on_write_mappings,
                        restoring=True,
                    ) for name in sorted(self.keys)
                })
            else:
                vars(self.environment).update(deepcopy(saved))
        if receipt is not None:
            identity_fields = (
                "_combat_event_identity", "_public_card_play_event_identity",
                "_phase_event_identity", "_visibility_event_identity",
                "_remaining_event_identity",
            )
            current = (receipt.generation, receipt.state_epoch)
            for name in identity_fields:
                if getattr(self.environment, name, None) is not None:
                    setattr(self.environment, name, current)
            # Rich telemetry from the pre-restore world must not be merged
            # with the new native identity.
            if hasattr(self.environment, "_rich_cache_identity"):
                self.environment._rich_cache_identity = None
                self.environment._rich_cache = None


@dataclass(frozen=True)
class RestorableSearchCheckpoint:
    """Explicitly restorable root state owned by one adapter instance."""

    state: SearchState
    root_capsule: Any = None
    checkpoint_capsule: Any = None

    def __post_init__(self) -> None:
        if not self.state.native_snapshot.get("handle"):
            raise ValueError("restorable checkpoint requires a native handle")


@dataclass(frozen=True)
class NativeRestoreReceipt:
    generation: int
    state_epoch: int
    tick: int
    native_digest: str

    @classmethod
    def from_observation(cls, observation: Mapping[str, Any], digest: str) -> "NativeRestoreReceipt":
        raw = observation.get("restore", observation)
        try:
            return cls(int(raw["generation"]), int(raw["stateEpoch"]), int(raw["tick"]), digest)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("native restore receipt is incomplete") from error


class NativeSnapshotSearchAdapter:
    """Own native handles and restore every non-native branch component.

    capture() returns a SearchState with an empty native_snapshot; it must
    contain a complete, untruncated native observation and legal actions.
    restore_sidecar() reinstates BattleEnv, reward ledger, event cursor and
    recurrent policy memory before the next action is applied.
    """

    def __init__(self, native: Any, *, capture: Callable[[], SearchState],
                 restore_sidecar: Callable[[SearchState], None],
                 apply: Callable[[str], None],
                 advance: Callable[[int, float], tuple[float, int, float]],
                 capture_view: Callable[[], SearchView] | None = None,
                 battle_sidecar: BattleEnvSidecar | None = None,
                 capture_root_capsule: Callable[[], Any] | None = None,
                 restore_root_capsule: Callable[[Any, NativeRestoreReceipt], None] | None = None,
                 capture_checkpoint: Callable[[], SearchState] | None = None,
                 capture_checkpoint_capsule: Callable[[], Any] | None = None,
                 restore_checkpoint_capsule: Callable[[Any, NativeRestoreReceipt], None] | None = None,
                 copy_capture: bool = True, validate_state: bool = True,
                 verify_restore_digest: bool = True,
                 fast_restore_atomic: bool = False,
                 profile_enabled: bool = False):
        self.native = native
        self.capture = capture
        self.restore_sidecar = restore_sidecar
        self.apply = apply
        self.advance = advance
        self.capture_view = capture_view
        self.battle_sidecar = battle_sidecar
        self.capture_root_capsule = capture_root_capsule
        self.restore_root_capsule = restore_root_capsule
        self.capture_checkpoint = capture_checkpoint
        self.capture_checkpoint_capsule = capture_checkpoint_capsule
        self.restore_checkpoint_capsule = restore_checkpoint_capsule
        self.copy_capture = copy_capture
        self.validate_state = validate_state
        self.verify_restore_digest = verify_restore_digest
        self.fast_restore_atomic = bool(fast_restore_atomic)
        self.profile_enabled = profile_enabled
        self._battle_states: dict[int, dict[str, Any]] = {}
        self._owned: set[int] = set()
        self._active: SearchState | None = None
        self._state_digest_cache: dict[int, str] = {}
        self.profile: dict[str, float] = {}
        self.profile_events: list[dict[str, Any]] = []
        self.profile_root_id: str | None = None
        self.profile_context: dict[str, Any] = {}
        self.search_profile: SearchProfile | None = None
        self._replay_schedule_dirty = False

    def _restore_native(self, handle: int) -> Mapping[str, Any]:
        if self.fast_restore_atomic:
            return self.native.restore(handle, validate_atomic=False)
        return self.native.restore(handle)

    def begin_profile_root(self, root_id: str, simulation_count: int = 0) -> None:
        self.profile.clear()
        self.profile_events.clear()
        self.profile_root_id = root_id
        self.search_profile = SearchProfile(root_id, simulation_count)
        self.profile_context = {"simulation_index": None, "edge_index": None,
                                "depth": None, "actor": None}

    def set_profile_context(self, *, simulation_index: int, edge_index: int,
                            depth: int, actor: int | None) -> None:
        self.profile_context = {
            "simulation_index": simulation_index,
            "edge_index": edge_index,
            "depth": depth,
            "actor": actor,
        }

    def _add_profile(self, name: str, started: float) -> None:
        if not self.profile_enabled:
            return
        elapsed = (time.perf_counter() - started) * 1000.0
        self.profile[name] = self.profile.get(name, 0.0) + elapsed
        if self.search_profile is not None:
            self.search_profile.append(name, elapsed, len(self._owned))
        self.profile_events.append({"root_id": self.profile_root_id, **self.profile_context,
                                    "span": name, "elapsed_ms": elapsed,
                                    "owned_snapshot_count": len(self._owned)})

    def _count_profile(self, name: str, amount: int = 1) -> None:
        if self.search_profile is not None:
            self.search_profile.count(name, amount)

    def _max_profile(self, name: str, value: int) -> None:
        if self.search_profile is not None:
            self.search_profile.set_max(name, value)

    def profile_summary(self) -> dict[str, Any]:
        summary = (self.search_profile.summary() if self.search_profile is not None else
                   {"root_id": self.profile_root_id, "simulation_count": 0,
                    "exclusive_spans_ms": dict(self.profile)})
        summary.update(
            schema="firstlight-search-profile.v1",
            span_count=len(self.profile_events),
            max_owned_snapshot_count=max(
                (event["owned_snapshot_count"] for event in self.profile_events), default=len(self._owned)
            ),
        )
        counters = dict(summary.get("counters", {}))
        for name in (
            "simulation_count", "edge_traversals", "edge_first_expansions", "edge_replays",
            "max_search_depth", "sum_search_depth", "checkpoint_hits", "checkpoint_misses",
            "checkpoint_evictions", "checkpoint_peak_owned", "policy_prior_calls",
            "opponent_prior_calls", "leaf_value_calls", "model_context_encode_calls",
            "model_evaluate_action_calls", "legal_action_count_sum",
            "prior_action_materialized_count", "prior_tensor_to_cpu_elements",
            "canonical_action_string_count", "search_recorder_decide_calls",
            "search_recorder_close_calls", "search_recorder_reconcile_calls", "state_key_calls",
            "state_key_existing_edge_calls", "state_key_new_edge_calls",
            "edge_memo_known_replays", "edge_memo_verifications",
            "edge_memo_viewless_replays", "edge_memo_mismatches",
            "edge_memo_checkpoint_captures", "edge_memo_checkpoint_replays",
        ):
            counters.setdefault(name, 0)
        summary["counters"] = counters
        edge_counts: dict[int, int] = {}
        for event in self.profile_events:
            if event["span"] != "advance_wall_ms":
                continue
            simulation_index = event["simulation_index"]
            if simulation_index is not None:
                edge_counts[simulation_index] = edge_counts.get(simulation_index, 0) + 1
        summary["edges_per_simulation"] = [
            edge_counts.get(index, 0) for index in range(summary["simulation_count"])
        ]
        return summary

    def _native_digest(self) -> str:
        result = self.native._request("state-digest")
        digest = result.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("native state digest missing")
        return digest

    def save_search_state(self, capture: Callable[[], SearchState] | None = None) -> SearchState:
        capture_started = time.perf_counter()
        capture_fn = self.capture if capture is None else capture
        frame = deepcopy(capture_fn()) if self.copy_capture else capture_fn()
        self._add_profile("sidecar_capture_ms", capture_started)
        if frame.native_snapshot:
            raise ValueError("capture callback must not supply a native snapshot")
        if frame.observation.get("truncated") is not False:
            raise ValueError("search requires an untruncated complete observation")
        battle_started = time.perf_counter()
        battle_state = self.battle_sidecar.capture() if self.battle_sidecar is not None else None
        self._add_profile("battle_sidecar_capture_ms", battle_started)
        snapshot_started = time.perf_counter()
        snapshot = self.native.create_snapshot()
        self._add_profile("snapshot_create_ms", snapshot_started)
        handle = snapshot.get("handle")
        if type(handle) is not int or handle <= 0:
            raise ValueError("native snapshot handle missing")
        self._owned.add(handle)
        if battle_state is not None:
            self._battle_states[handle] = battle_state
        try:
            digest_started = time.perf_counter()
            native_sha = self._native_digest()
            self._add_profile("state_digest_rpc_ms", digest_started)
            observe_started = time.perf_counter()
            observed_tick = self.native.observe().get("tick")
            self._add_profile("observe_rpc_ms", observe_started)
            if snapshot.get("tick") != observed_tick:
                raise ValueError("native state changed during snapshot capture")
            if frame.observation.get("tick") != snapshot.get("tick"):
                raise ValueError("observation and native snapshot ticks differ")
            state = replace(frame, native_snapshot={**deepcopy(snapshot), "state_sha256": native_sha})
            if self.validate_state:
                digest_started = time.perf_counter()
                semantic_state_digest = state_digest(state)
                self._add_profile("python_state_digest_ms", digest_started)
                self._state_digest_cache[id(state)] = semantic_state_digest
            self._active = state
            return state
        except BaseException:
            self.release_search_state(replace(frame, native_snapshot=snapshot))
            raise

    def restore_search_state(self, state: SearchState) -> None:
        handle = state.native_snapshot.get("handle")
        if handle not in self._owned:
            raise ValueError("native snapshot is not owned by this search")
        self._active = None
        self._clear_replay_schedule_if_needed(state.external_queue)
        restore_started = time.perf_counter()
        restored = self._restore_native(handle)
        self._add_profile("restore_rpc_ms", restore_started)
        restored_digest = (self._native_digest() if self.verify_restore_digest
                           else state.native_snapshot["state_sha256"])
        receipt = NativeRestoreReceipt.from_observation(restored, restored_digest)
        if self.verify_restore_digest and restored_digest != state.native_snapshot.get("state_sha256"):
            raise ValueError("native snapshot digest changed after restore")
        if self.battle_sidecar is not None:
            battle_started = time.perf_counter()
            self.battle_sidecar.restore(self._battle_states[handle], receipt=receipt)
            self._add_profile("battle_sidecar_restore_ms", battle_started)
        sidecar_started = time.perf_counter()
        self.restore_sidecar(state)
        self._add_profile("sidecar_restore_ms", sidecar_started)
        schedule_started = time.perf_counter()
        self._schedule_external_queue(state.external_queue)
        self._add_profile("replay_schedule_ms", schedule_started)
        self._active = state

    def _clear_replay_schedule_if_needed(self, entries: Any) -> None:
        if entries or self._replay_schedule_dirty:
            self.native.clear_replay_schedule()
            self._replay_schedule_dirty = False

    def _schedule_external_queue(self, entries: Any) -> None:
        for entry in entries:
            kind = entry.get("kind")
            if kind == "card":
                self.native.schedule_replay_card_at_tick(**{key: entry[key] for key in
                    ("owner", "card_id", "x", "y", "execute_tick")})
            elif kind == "ability":
                self.native.schedule_replay_ability_at_tick(**{key: entry[key] for key in
                    ("owner", "ability_name_hints", "execute_tick")})
            else:
                raise ValueError("unsupported external queue entry")
            self._replay_schedule_dirty = True

    def checkpoint_restorable(self) -> RestorableSearchCheckpoint:
        state = self.save_search_state()
        capsule = (self.capture_root_capsule()
                   if self.capture_root_capsule is not None else None)
        return RestorableSearchCheckpoint(state, capsule)

    def view_from_checkpoint(self, checkpoint: RestorableSearchCheckpoint) -> SearchView:
        if not isinstance(checkpoint, RestorableSearchCheckpoint):
            raise TypeError("root view requires RestorableSearchCheckpoint")
        state = checkpoint.state
        handle = state.native_snapshot.get("handle")
        if handle not in self._owned or self._active is not state:
            raise ValueError("root checkpoint is not the active search state")
        return SearchView.from_state(
            state, execution_digest=state.native_snapshot["state_sha256"]
        )

    def restore(self, checkpoint: RestorableSearchCheckpoint) -> None:
        if not isinstance(checkpoint, RestorableSearchCheckpoint):
            raise TypeError("restore requires RestorableSearchCheckpoint")
        if checkpoint.root_capsule is not None and self.restore_root_capsule is not None:
            self._restore_capsule_checkpoint(
                checkpoint, checkpoint.root_capsule, self.restore_root_capsule,
                "root_capsule_restore_ms")
            return
        if (checkpoint.checkpoint_capsule is not None
                and self.restore_checkpoint_capsule is not None):
            self._restore_capsule_checkpoint(
                checkpoint, checkpoint.checkpoint_capsule,
                self.restore_checkpoint_capsule, "checkpoint_capsule_restore_ms")
            return
        self.restore_search_state(checkpoint.state)

    def _restore_capsule_checkpoint(
            self, checkpoint: RestorableSearchCheckpoint, capsule: Any,
            restore_capsule: Callable[[Any, NativeRestoreReceipt], None],
            profile_name: str) -> None:
            state = checkpoint.state
            handle = state.native_snapshot.get("handle")
            if handle not in self._owned:
                raise ValueError("checkpoint is not owned by this search")
            self._active = None
            self._clear_replay_schedule_if_needed(state.external_queue)
            restore_started = time.perf_counter()
            restored = self._restore_native(handle)
            self._add_profile("restore_rpc_ms", restore_started)
            restored_digest = (self._native_digest() if self.verify_restore_digest
                               else state.native_snapshot["state_sha256"])
            receipt = NativeRestoreReceipt.from_observation(restored, restored_digest)
            if self.verify_restore_digest and restored_digest != state.native_snapshot.get("state_sha256"):
                raise ValueError("native snapshot digest changed after root restore")
            if self.battle_sidecar is not None:
                battle_started = time.perf_counter()
                self.battle_sidecar.restore(self._battle_states[handle], receipt=receipt)
                self._add_profile("battle_sidecar_restore_ms", battle_started)
            capsule_started = time.perf_counter()
            restore_capsule(capsule, receipt)
            self._add_profile(profile_name, capsule_started)
            schedule_started = time.perf_counter()
            self._schedule_external_queue(state.external_queue)
            self._add_profile("replay_schedule_ms", schedule_started)
            self._active = state

    def release(self, checkpoint: RestorableSearchCheckpoint) -> None:
        if not isinstance(checkpoint, RestorableSearchCheckpoint):
            raise TypeError("release requires RestorableSearchCheckpoint")
        self.release_search_state(checkpoint.state)

    def capture_current_view(self) -> SearchView:
        capture_started = time.perf_counter()
        if self.capture_view is None:
            frame = deepcopy(self.capture()) if self.copy_capture else self.capture()
            self._add_profile("sidecar_view_capture_ms", capture_started)
            if frame.native_snapshot:
                raise ValueError("current view capture must not supply a native snapshot")
            if frame.observation.get("truncated") is not False:
                raise ValueError("search requires an untruncated complete observation")
            view = SearchView.from_state(frame)
        else:
            view = self.capture_view()
            self._add_profile("sidecar_view_capture_ms", capture_started)
            if not isinstance(view, SearchView):
                raise TypeError("capture_view must return SearchView")
        if view.observation.get("truncated") is not False:
            raise ValueError("search requires an untruncated complete observation")
        digest_started = time.perf_counter()
        execution_digest = self._native_digest()
        self._add_profile("state_digest_rpc_ms", digest_started)
        view_started = time.perf_counter()
        view = replace(view, execution_digest=execution_digest)
        self._add_profile("view_materialize_ms", view_started)
        return view

    def advance_current_until_decision(self, max_decision_points: int,
                                       max_native_seconds: float) -> Transition:
        started = time.perf_counter()
        reward, decisions, seconds = self.advance(max_decision_points, max_native_seconds)
        self._add_profile("advance_wall_ms", started)
        view = self.capture_current_view()
        return Transition(view, reward, decisions, seconds)

    def advance_current_without_view(self, max_decision_points: int,
                                     max_native_seconds: float) -> Transition:
        started = time.perf_counter()
        reward, decisions, seconds = self.advance(max_decision_points, max_native_seconds)
        self._add_profile("advance_wall_ms", started)
        return Transition(None, reward, decisions, seconds)

    def checkpoint_current(self) -> RestorableSearchCheckpoint:
        state = self.save_search_state(self.capture_checkpoint)
        capsule = (self.capture_checkpoint_capsule()
                   if self.capture_checkpoint_capsule is not None else None)
        return RestorableSearchCheckpoint(state, checkpoint_capsule=capsule)

    def apply_action(self, action: str) -> None:
        self._active = None
        self.apply(action)

    def advance_until_decision(self, max_decision_points: int,
                               max_native_seconds: float) -> Transition:
        reward, decisions, seconds = self.advance(max_decision_points, max_native_seconds)
        return Transition(self.save_search_state(), reward, decisions, seconds)

    def fingerprint(self) -> str:
        if self._active is None:
            raise ValueError("fingerprint requires a restored search state")
        native_digest = self._native_digest()
        if native_digest != self._active.native_snapshot.get("state_sha256"):
            raise ValueError("native state diverged from saved search state")
        if self.validate_state:
            search = self._state_digest_cache.get(id(self._active))
            if search is None:
                digest_started = time.perf_counter()
                search = state_digest(self._active)
                self._add_profile("python_state_digest_ms", digest_started)
                self._state_digest_cache[id(self._active)] = search
        else:
            search = {"tick": self._active.observation.get("tick"),
                      "actor": self._active.actor,
                      "legal": self._active.legal_actions}
        return _digest({"native": native_digest, "search": search})

    def release_search_state(self, state: SearchState) -> None:
        handle = state.native_snapshot.get("handle")
        if handle in self._owned:
            started = time.perf_counter()
            self.native.release_snapshot(handle)
            self._owned.remove(handle)
            self._add_profile("snapshot_release_ms", started)
            self._battle_states.pop(handle, None)
        if self._active is state:
            self._active = None
        self._state_digest_cache.pop(id(state), None)

    @property
    def owned_snapshot_count(self) -> int:
        return len(self._owned)
