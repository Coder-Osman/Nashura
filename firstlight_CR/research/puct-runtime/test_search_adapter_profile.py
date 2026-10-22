import hashlib
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from native_runner.contracts import EventV1
from tree_search import SearchState, SearchView


class FakeNative:
    def __init__(self):
        self.tick = 3
        self.handles = {}
        self.next_handle = 1
        self.clear_calls = 0
        self.call_log = []
        self.replay_schedule = []

    def _request(self, command):
        assert command == "state-digest"
        return {"sha256": hashlib.sha256(str(self.tick).encode()).hexdigest()}

    def observe(self):
        return {"tick": self.tick, "generation": 1, "stateEpoch": 7}

    def create_snapshot(self):
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = self.tick
        return {"handle": handle, "tick": self.tick}

    def restore(self, handle):
        self.call_log.append("restore")
        self.tick = self.handles[handle]
        return self.observe()

    def release_snapshot(self, handle):
        del self.handles[handle]

    def clear_replay_schedule(self):
        self.call_log.append("clear")
        self.clear_calls += 1
        self.replay_schedule.clear()

    def schedule_replay_card_at_tick(self, **entry):
        self.call_log.append("schedule")
        self.replay_schedule.append(entry)


def _capture_state(native):
    return SearchState(
        native_snapshot={}, observation={"tick": native.tick, "truncated": False},
        actor=0, legal_actions=("wait",), external_queue=(), reward_ledger={},
        event_cursor={}, policy_state={},
    )


def _adapter_for_restore_test(native):
    return NativeSnapshotSearchAdapter(
        native, capture=lambda: _capture_state(native),
        restore_sidecar=lambda _: None, apply=lambda _: None,
        advance=lambda *_: (0., 1, .1),
    )


def test_restore_skips_replay_clear_for_empty_external_queue():
    native = FakeNative()
    adapter = _adapter_for_restore_test(native)
    checkpoint = adapter.checkpoint_restorable()
    native.call_log.clear()

    adapter.restore_search_state(checkpoint.state)

    assert native.clear_calls == 0
    assert native.call_log == ["restore"]
    adapter.release(checkpoint)


def test_restore_clears_before_replaying_external_queue():
    native = FakeNative()
    adapter = _adapter_for_restore_test(native)
    checkpoint = adapter.checkpoint_restorable()
    state = replace(checkpoint.state, external_queue=(
        {"kind": "card", "owner": 0, "card_id": 7, "x": 1, "y": 2,
         "execute_tick": 9},
    ))
    native.call_log.clear()

    adapter.restore_search_state(state)

    assert native.clear_calls == 1
    assert native.call_log == ["clear", "restore", "schedule"]
    assert native.replay_schedule == [{
        "owner": 0, "card_id": 7, "x": 1, "y": 2, "execute_tick": 9,
    }]
    adapter.release(checkpoint)


def test_restore_clears_stale_schedule_before_empty_queue_restore():
    native = FakeNative()
    adapter = _adapter_for_restore_test(native)
    checkpoint = adapter.checkpoint_restorable()
    queued_state = replace(checkpoint.state, external_queue=(
        {"kind": "card", "owner": 0, "card_id": 7, "x": 1, "y": 2,
         "execute_tick": 9},
    ))

    adapter.restore_search_state(queued_state)
    native.call_log.clear()
    adapter.restore_search_state(checkpoint.state)

    assert native.clear_calls == 2
    assert native.call_log == ["clear", "restore"]
    assert native.replay_schedule == []
    adapter.release(checkpoint)


def test_root_restore_clears_stale_schedule_before_empty_queue_restore():
    native = FakeNative()
    adapter = NativeSnapshotSearchAdapter(
        native, capture=lambda: _capture_state(native), restore_sidecar=lambda _: None,
        apply=lambda _: None, advance=lambda *_: (0., 1, .1),
        capture_root_capsule=lambda: object(),
        restore_root_capsule=lambda *_: None,
    )
    checkpoint = adapter.checkpoint_restorable()
    queued_checkpoint = replace(checkpoint, state=replace(checkpoint.state, external_queue=(
        {"kind": "card", "owner": 0, "card_id": 7, "x": 1, "y": 2,
         "execute_tick": 9},
    )))

    adapter.restore(queued_checkpoint)
    native.call_log.clear()
    adapter.restore(checkpoint)

    assert native.clear_calls == 2
    assert native.call_log == ["clear", "restore"]
    assert native.replay_schedule == []
    adapter.release(checkpoint)


def test_adapter_profile_is_root_scoped_and_tracks_snapshot_lifecycle():
    native = FakeNative()

    def capture():
        return SearchState(
            native_snapshot={}, observation={"tick": native.tick, "truncated": False},
            actor=0, legal_actions=("wait",), external_queue=(), reward_ledger={},
            event_cursor={}, policy_state={},
        )

    view = SearchView(
        observation={"tick": 3, "truncated": False}, actor=0,
        legal_actions=("wait",), external_queue=(), reward_ledger={},
        event_cursor={}, policy_state={},
    )
    adapter = NativeSnapshotSearchAdapter(
        native, capture=capture, restore_sidecar=lambda _: None,
        apply=lambda _: None, advance=lambda *_: (0., 1, .1),
        capture_view=lambda: view, profile_enabled=True,
    )

    adapter.begin_profile_root("episode:0", 32)
    adapter.capture_current_view()
    checkpoint = adapter.checkpoint_restorable()
    adapter.restore(checkpoint)
    adapter.release(checkpoint)
    summary = adapter.profile_summary()
    assert summary["root_id"] == "episode:0"
    assert summary["simulation_count"] == 32
    assert summary["exclusive_spans_ms"]["snapshot_create_ms"] >= 0
    assert summary["exclusive_spans_ms"]["snapshot_release_ms"] >= 0
    assert summary["max_owned_snapshot_count"] == 1
    assert summary["edges_per_simulation"] == [0] * 32
    assert adapter.owned_snapshot_count == 0
    assert not native.handles

    adapter.begin_profile_root("episode:1", 32)
    assert adapter.profile_events == []
    assert adapter.profile_summary()["exclusive_spans_ms"] == {}


def test_snapshot_handle_is_released_when_post_create_digest_fails():
    native = FakeNative()

    def capture():
        return SearchState(
            native_snapshot={}, observation={"tick": native.tick, "truncated": False},
            actor=0, legal_actions=("wait",), external_queue=(), reward_ledger={},
            event_cursor={}, policy_state={},
        )

    adapter = NativeSnapshotSearchAdapter(
        native, capture=capture, restore_sidecar=lambda _: None,
        apply=lambda _: None, advance=lambda *_: (0., 1, .1),
    )
    native._request = lambda _: (_ for _ in ()).throw(TimeoutError("digest RPC timeout"))
    with pytest.raises(TimeoutError, match="digest RPC timeout"):
        adapter.checkpoint_restorable()
    assert adapter.owned_snapshot_count == 0
    assert not native.handles


def test_sidecar_restore_exception_requires_checkpoint_owner_to_release():
    native = FakeNative()

    def capture():
        return SearchState(
            native_snapshot={}, observation={"tick": native.tick, "truncated": False},
            actor=0, legal_actions=("wait",), external_queue=(), reward_ledger={},
            event_cursor={}, policy_state={},
        )

    def broken_restore(_):
        raise RuntimeError("sidecar restore failed")

    adapter = NativeSnapshotSearchAdapter(
        native, capture=capture, restore_sidecar=broken_restore,
        apply=lambda _: None, advance=lambda *_: (0., 1, .1),
    )
    checkpoint = adapter.checkpoint_restorable()
    with pytest.raises(RuntimeError, match="sidecar restore failed"):
        adapter.restore(checkpoint)
    adapter.release(checkpoint)
    assert adapter.owned_snapshot_count == 0
    assert not native.handles


def test_profiled_battle_sidecar_preserves_cross_field_aliases():
    shared = ["root"]
    environment = SimpleNamespace(
        _raw={"shared": shared}, _events=shared, _pending=[],
        _last_rewards={}, _last_potential=0.0,
    )
    sidecar = BattleEnvSidecar(environment)
    spans = []
    sidecar.profile_sink = lambda name, started: spans.append(name)

    saved = sidecar.capture()
    assert saved["_raw"]["shared"] is saved["_events"]
    shared.append("branch")
    sidecar.restore(saved)
    assert environment._raw["shared"] is environment._events
    assert environment._events == ["root"]
    environment._events.append("next branch")
    assert saved["_events"] == ["root"]
    assert "battle_capture__events_ms" in spans
    assert "battle_restore__events_ms" in spans


def test_battle_sidecar_discards_epoch_bound_runtime_cache():
    environment = SimpleNamespace(
        _raw={"tick": 10}, _events=[], _pending=[],
        _last_rewards={}, _last_potential=0.0,
        _action_player_runtime_cache={(10, 1, 2, 0): {"runtime": "old"}},
    )
    sidecar = BattleEnvSidecar(environment, discard_epoch_cache=True)
    saved = sidecar.capture()
    assert saved["_action_player_runtime_cache"] == {}

    environment._action_player_runtime_cache[(11, 2, 3, 0)] = {"runtime": "branch"}
    sidecar.restore(saved)
    assert environment._action_player_runtime_cache == {}
    assert environment._raw == {"tick": 10}

    saved["_action_player_runtime_cache"] = {(10, 1, 2, 0): "unexpected"}
    with pytest.raises(ValueError, match="epoch-bound runtime cache"):
        sidecar.restore(saved)


def test_battle_sidecar_shares_immutable_caches_but_copies_mutable_containers():
    environment = SimpleNamespace(
        _raw={"tick": 10}, _events=deque(), _pending=[], _last_rewards={}, _last_potential=0.0,
        _fair_context_cache=SimpleNamespace(snapshot="immutable"),
        _fair_scene_cache=("context", (), (), 0),
        _causal_group_by_entity={1: "immutable-ref"},
        _revealed={0: {1}, 1: set()},
        _step_action_outcomes=[], _trace=[], _public_metadata_static={"static": True},
    )
    sidecar = BattleEnvSidecar(environment, share_immutable_events=True)
    saved = sidecar.capture()

    assert saved["_fair_context_cache"] is environment._fair_context_cache
    assert saved["_fair_scene_cache"] is environment._fair_scene_cache
    assert saved["_causal_group_by_entity"] is not environment._causal_group_by_entity
    assert saved["_revealed"] is not environment._revealed
    assert saved["_revealed"][0] is not environment._revealed[0]
    assert saved["_step_action_outcomes"] is not environment._step_action_outcomes

    environment._causal_group_by_entity[2] = "branch-only"
    environment._revealed[0].add(2)
    environment._step_action_outcomes.append("branch-only")
    assert 2 not in saved["_causal_group_by_entity"]
    assert 2 not in saved["_revealed"][0]
    assert saved["_step_action_outcomes"] == []


def test_search_sidecar_shares_immutable_event_entries_but_not_event_ring():
    event = EventV1(tick=90, event_type="action_executed")
    environment = SimpleNamespace(
        _raw={"tick": 90}, _events=deque((event,), maxlen=8),
        _step_action_outcomes=[event], _pending=[], _last_rewards={}, _last_potential=0.0,
    )
    sidecar = BattleEnvSidecar(environment, share_immutable_events=True)
    saved = sidecar.capture()
    assert saved["_events"] is not environment._events
    assert saved["_events"][0] is saved["_step_action_outcomes"][0]

    environment._events.append(EventV1(tick=91, event_type="spawn"))
    sidecar.restore(saved)
    assert len(environment._events) == 1
    assert environment._events[0] is environment._step_action_outcomes[0]
    environment._events.append(EventV1(tick=92, event_type="despawn"))
    assert len(saved["_events"]) == 1

    environment._events = deque((object(),))
    with pytest.raises(ValueError, match="immutable EventV1"):
        sidecar.capture()


def test_search_sidecar_event_ring_defers_copy_and_preserves_deque_contract():
    events = tuple(EventV1(tick=tick, event_type=f"event_{tick}") for tick in range(3))
    environment = SimpleNamespace(
        _raw={"tick": 3}, _events=deque(events, maxlen=3), _pending=[],
        _last_rewards={}, _last_potential=0.0,
    )
    sidecar = BattleEnvSidecar(environment, share_immutable_events=True)
    saved = sidecar.capture()
    sidecar.restore(saved)

    assert len(environment._events) == 3
    assert environment._events.maxlen == 3
    assert environment._events[0] is events[0]
    assert tuple(environment._events) == events

    new_event = EventV1(tick=3, event_type="event_3")
    environment._events.append(new_event)
    assert tuple(environment._events) == (events[1], events[2], new_event)
    assert tuple(saved["_events"]) == events
    assert saved["_events"][0] is events[0]

    environment._events.clear()
    assert len(environment._events) == 0
    assert tuple(saved["_events"]) == events


def test_battle_sidecar_copy_on_write_restore_isolated_from_root():
    environment = SimpleNamespace(
        _raw={"tick": 10}, _events=deque(), _pending=[], _last_rewards={}, _last_potential=0.0,
        _causal_group_by_entity={1: "root"},
        _combat_event_kind_by_sequence={2: "spawn"},
        _last_potential_by_owner={0: 0.0},
    )
    sidecar = BattleEnvSidecar(
        environment, share_immutable_events=True, copy_on_write_mappings=True,
    )
    saved = sidecar.capture()
    sidecar.restore(saved)

    assert environment._causal_group_by_entity[1] == "root"
    assert environment._causal_group_by_entity is not saved["_causal_group_by_entity"]
    environment._causal_group_by_entity[2] = "branch"
    environment._causal_group_by_entity.pop(1)
    environment._combat_event_kind_by_sequence.clear()
    assert saved["_causal_group_by_entity"] == {1: "root"}
    assert saved["_combat_event_kind_by_sequence"] == {2: "spawn"}


def test_battle_sidecar_copy_on_write_supports_mapping_reads_and_replacement():
    environment = SimpleNamespace(
        _raw={"tick": 10}, _events=deque(), _pending=[], _last_rewards={}, _last_potential=0.0,
        _causal_group_by_entity={1: "root"},
    )
    sidecar = BattleEnvSidecar(
        environment, share_immutable_events=True, copy_on_write_mappings=True,
    )
    saved = sidecar.capture()
    sidecar.restore(saved)
    assert list(environment._causal_group_by_entity.items()) == [(1, "root")]
    environment._causal_group_by_entity.update({2: "branch"})
    assert environment._causal_group_by_entity.copy() == {1: "root", 2: "branch"}
    assert saved["_causal_group_by_entity"] == {1: "root"}
