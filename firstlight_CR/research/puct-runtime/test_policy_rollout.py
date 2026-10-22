import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import ActionV1
from native_runner.training.tracking import AvailabilityIndex, PublicCardState
from native_runner.training.v4.tensors import RecurrentPolicyStateV4
from policy_rollout import (
    DualPolicyRollout, _decode, _encode, _EPISODE_FIELDS, _TRACKER_FIELDS,
    _tensorizer_snapshot, _restore_tensorizer,
)


def sessions():
    result = {}
    for owner in (0, 1):
        tracker = SimpleNamespace(**{name: {} for name in _TRACKER_FIELDS})
        tracker._card_states = {owner: {26000021: PublicCardState(AvailabilityIndex.AVAILABLE, 0, True)}}
        tracker._seen_events = {(90, "action_executed", owner, None)}
        tensorizer = SimpleNamespace(**{name: None for name in _EPISODE_FIELDS}, tracker=tracker)
        result[owner] = SimpleNamespace(
            device=torch.device("cpu"), tensorizer=tensorizer,
            state=RecurrentPolicyStateV4(hidden=torch.ones(1, 1, 4) * owner, cell=torch.zeros(1, 1, 4)),
            _episode_started=True, _first_decision=False)
    return result


def rollout():
    result = DualPolicyRollout(None, sessions())
    result._episode_started = True
    return result


def test_json_roundtrip_preserves_tracker_types_and_tensor_dtype():
    original = rollout()
    original.set_actions(1, (ActionV1.wait(1, ticks=5),))
    state = json.loads(json.dumps(original.capture_search_policy_state(), allow_nan=False))
    original.sessions[0].tensorizer.tracker._seen_events.clear()
    original.restore_policy_state(state)
    tracker = original.sessions[0].tensorizer.tracker
    assert type(tracker._card_states[0][26000021]) is PublicCardState
    assert tracker._card_states[0][26000021].availability is AvailabilityIndex.AVAILABLE
    assert tracker._seen_events == {(90, "action_executed", 0, None)}
    assert original.actions()[1] == (ActionV1.wait(1, ticks=5),)
    assert original.sessions[1].state.hidden.dtype == torch.float32


def test_restores_are_independent_of_saved_tensor_storage():
    original = rollout()
    state = original.capture_policy_state()
    original.restore_policy_state(state)
    original.sessions[1].state.hidden.add_(20)
    original.restore_policy_state(state)
    assert torch.equal(original.sessions[1].state.hidden, torch.ones(1, 1, 4))


def test_sampling_rng_repeats_after_json_restore():
    original = rollout()
    state = json.loads(json.dumps(original.capture_search_policy_state()))
    expected = torch.rand(32)
    torch.rand(100)
    original.restore_policy_state(state)
    assert torch.equal(torch.rand(32), expected)


def test_shared_graph_outputs_do_not_alias_other_owner_state():
    original = rollout()
    shared = RecurrentPolicyStateV4(hidden=torch.ones(1, 1, 4), cell=torch.zeros(1, 1, 4))

    def decide(_observation):
        original.sessions[0].state = shared
        return SimpleNamespace(decoded=SimpleNamespace(actions=()))

    original.sessions[0].decide = decide
    original.decide(0, None)
    shared.hidden.add_(7)
    assert torch.equal(original.sessions[0].state.hidden, torch.ones(1, 1, 4))


def test_branch_restores_queues_ledger_and_event_cursor():
    original = rollout()
    original.event_cursor = {"combat": 20}
    original.reward_ledger = {"pending": [1, 2]}
    original.external_queue = [{"kind": "card", "execute_tick": 99}]
    state = original.capture_search_policy_state()
    original.event_cursor["combat"] = 23
    original.reward_ledger["pending"].append(3)
    original.external_queue.clear()
    original.restore_policy_state(state)
    assert original.event_cursor == {"combat": 20}
    assert original.reward_ledger == {"pending": [1, 2]}
    assert original.external_queue == [{"kind": "card", "execute_tick": 99}]


def test_wrong_tensor_shape_fails():
    state = _encode(torch.ones(1, 2))
    state["shape"] = [2, 1]
    with pytest.raises(ValueError, match="shape mismatch"):
        _decode(state)


def test_seen_event_snapshot_uses_independent_sets_of_immutable_keys():
    tensorizer = sessions()[0].tensorizer
    original = (90, "action_executed", 0, None)
    saved = _tensorizer_snapshot(tensorizer)
    tensorizer.tracker._seen_events.add((91, "action_executed", 0, None))
    assert saved["tracker"]["_seen_events"] == {original}

    _restore_tensorizer(tensorizer, saved)
    tensorizer.tracker._seen_events.add((92, "action_executed", 0, None))
    assert saved["tracker"]["_seen_events"] == {original}

    tensorizer.tracker._seen_events = {(90, "action_executed", 0, (1, 2))}
    with pytest.raises(ValueError, match="immutable scalar tuples"):
        _tensorizer_snapshot(tensorizer)
