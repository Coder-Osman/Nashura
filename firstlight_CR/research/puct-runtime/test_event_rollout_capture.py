from types import SimpleNamespace
from unittest.mock import patch

import event_policy_rollout
from recorder_capsule import SearchRecorderState
from tree_search import SearchState, state_digest
import torch

from event_policy_rollout import EventPolicyRollout, _STATE_FIELDS
from test_recorder_capsule import Recorder


def make_rollout():
    rollout = object.__new__(EventPolicyRollout)
    values = {
        "plans": {0: None, 1: None},
        "ledger": {"pending": {}},
        "recovery_owners": set(),
        "event_ticks": set(),
        "seen_events": set(),
        "last_decision_by_owner": {0: 0, 1: 0},
        "ready": [0],
        "frames": {0: None, 1: None},
        "legals": {0: None, 1: None},
        "tick": 100,
        "terminal": False,
        "recorder": Recorder(),
        "totals": {0: 0., 1: 0.},
        "decisions": 0,
        "steps": 0,
        "cancellations": 0,
        "native_events": 0,
        "initial_tower_hp": (),
    }
    assert set(values) == set(_STATE_FIELDS)
    for name, value in values.items():
        setattr(rollout, name, value)
    rollout.recorder._pending = {0: None, 1: None}
    rollout.recorder._submitted = {}
    rollout.tensorizers = {}
    rollout.overflow = None
    rollout.device = torch.device("cpu")
    rollout.started = True
    rollout._search_recorder_capsule = None
    rollout._search_execution_ledger = None
    rollout.environment = SimpleNamespace(native=SimpleNamespace(
        observe=lambda: {"stateEpoch": 7}
    ))
    return rollout


def search_state(snapshot):
    return SearchState(
        native_snapshot={}, observation={"tick": 100, "truncated": False},
        actor=0, legal_actions=("wait",), external_queue=(),
        reward_ledger={}, event_cursor={}, policy_state={"rollout": snapshot},
    )


def test_search_capture_keeps_one_capsule_and_restores_branch():
    rollout = make_rollout()
    rollout.begin_search_root(search_only=False)
    captured = rollout.capture_search()
    recorder_state = captured["state"]["recorder"]
    assert isinstance(recorder_state, SearchRecorderState)
    assert recorder_state.capsule is rollout._search_recorder_capsule
    before = state_digest(search_state(captured))

    rollout.recorder._rows[0][0].status = "modified"
    rollout.tick = 200
    rollout.restore_search(captured)

    assert rollout.tick == 100
    assert rollout.recorder.rows[0][0].status == "pending"
    assert state_digest(search_state(rollout.capture_search())) == before
    rollout.audit_search_root()


def test_search_capture_branch_replacement_has_distinct_key():
    rollout = make_rollout()
    rollout.begin_search_root(search_only=False)
    original = state_digest(search_state(rollout.capture_search()))
    rollout.recorder._rows[0][0].status = "executed"
    changed = state_digest(search_state(rollout.capture_search()))
    assert changed != original


def test_search_restore_reuses_decoded_snapshot_without_aliasing():
    rollout = make_rollout()
    rollout.begin_search_root(search_only=False)
    captured = rollout.capture_search()

    with patch("event_policy_rollout._decode", side_effect=AssertionError("unexpected decode")):
        rollout.restore_search(captured)
    rollout.ready.append(1)
    rollout.restore_search(captured)

    assert rollout.ready == [0]
    rollout._search_restore_cache.clear()
    with patch("event_policy_rollout._decode", wraps=event_policy_rollout._decode) as decode:
        rollout.restore_search(captured)
    assert decode.call_count >= 1
