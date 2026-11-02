"""Bounded host history without changing FAIR windows or tracker results."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from native_runner.contracts import EventV1
from native_runner.tests.test_battle_env import FakeNativeEnv, _test_catalog, _reset
from native_runner.tests.test_training_tracking import _tracker, _observation
from native_runner.training import tracking
from native_runner.training.fair_final.native import FinalBattleEnv


def environment():
    env = FinalBattleEnv(native=FakeNativeEnv(), card_catalog=_test_catalog(),
                        ruleset_id="a" * 64, warmup_ticks=0)
    _reset(env)
    return env


@pytest.mark.parametrize("policy_only", [False, True])
def test_dual_uses_owner_windows_once_with_exact_single_view_results(policy_only):
    env = environment()
    for owner in (0, 1):
        env._append_event(EventV1(tick=env.current_tick, event_type="action_executed",
                                 owner=owner, card_id=101, data={"private_to": owner}))
    # A private flood can evict the global ring while public owner windows stay intact.
    expected = {owner: env.observe(owner=owner, policy_features_only=policy_only) for owner in (0, 1)}
    with patch.object(env, "_event_visible_to", side_effect=AssertionError("shared ring rescanned")), \
            patch.object(env, "_events_for", wraps=env._events_for) as windows, \
            patch.object(env, "_event_summary_for", wraps=env._event_summary_for) as summaries:
        actual = env.observe_dual(policy_features_only=policy_only)
    assert windows.call_count == 2
    assert summaries.call_count == (0 if policy_only else 2)
    for owner in (0, 1):
        # Wall-clock capture timestamps are deliberately not part of this equality.
        assert replace(actual[owner], time=expected[owner].time) == expected[owner]


def test_window_cache_same_tick_privacy_expiry_and_restore():
    env = environment()
    tick = env.current_tick
    public = EventV1(tick=tick, event_type="action_executed", owner=1, card_id=101)
    env._append_event(public)
    before = env._events_for(0, tick)
    assert env._events_for(0, tick) is before
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "research" / "puct-runtime"))
    from native_adapter import BattleEnvSidecar
    sidecar = BattleEnvSidecar(env)
    saved = sidecar.capture()
    env._append_event(EventV1(tick=tick, event_type="action_executed", owner=1,
                             card_id=102, data={"private_to": 1}))
    assert env._events_for(0, tick) is before
    new = EventV1(tick=tick, event_type="action_executed", owner=0, card_id=103)
    env._append_event(new)
    assert env._events_for(0, tick)[-1] == new
    assert env._events_for(0, tick + env.event_window_ticks + 1) == ()
    sidecar.restore(saved)
    assert env._events_for(0, tick) == before
    _reset(env)
    assert public not in env._events_for(0, env.current_tick)


def test_tracker_dedup_history_is_tick_bounded_and_restorable(monkeypatch):
    tracker = _tracker()
    plays = []
    for tick in range(1, 101):
        plays.append(EventV1(tick=tick, event_type="action_executed", owner=1,
                             card_id=101 + tick % 8))
        tracker.update(_observation(tick=tick, events=tuple(plays)))
        assert len(tracker._seen_events) == 1
        assert {identity[0] for identity in tracker._seen_events} == {tick}
    state = deepcopy(tracker.__dict__)
    identity_calls = []
    original = tracking._event_identity
    def checked_identity(event):
        identity_calls.append(event.tick)
        return original(event)
    monkeypatch.setattr(tracking, "_event_identity", checked_identity)
    tracker.update(_observation(tick=100, events=tuple(plays)))
    assert identity_calls == [100]
    new = EventV1(tick=100, event_type="action_executed", owner=1, card_id=108)
    tracker.update(_observation(tick=100, events=tuple(plays) + (new,)))
    assert len(tracker._seen_events) == 2
    expected = deepcopy(tracker.__dict__)
    tracker.__dict__ = deepcopy(state)
    tracker.update(_observation(tick=100, events=tuple(plays) + (new,)))
    assert tracker.__dict__ == expected
    tracker.update(_observation(tick=101, events=tuple(plays) + (new,)))
    assert tracker._seen_events == set()
    with pytest.raises(ValueError, match="precedes"):
        tracker.update(_observation(tick=100))


def phase_envelope(count=2):
    from native_runner.tests.test_native_runtime_telemetry_api import (
        _rich_response, _add_phase_runtime, _combat_fact)
    from native_runner.cr_native_env import PHASE_EVENT_HOOKS
    rich = _rich_response()
    _add_phase_runtime(rich)
    envelope = rich["phaseRuntime"]
    kind = next(iter(PHASE_EVENT_HOOKS))
    record = dict(sequence=1, tick=1, kind=kind, hookOffset=PHASE_EVENT_HOOKS[kind],
        callerOffset=None, entity=_combat_fact(rich["objects"][0]),
        targetBefore=_combat_fact(rich["objects"][0]),
        targetAfter=_combat_fact(rich["objects"][0]), buffGlobalId=None, buffRemainingMs=None,
        speedMultiplier=None, hitSpeedMultiplier=None, inputStep=None, outputStep=None,
        timelineBefore=None, timelineAfter=None, classicChargeBefore=None, classicChargeAfter=None,
        success=True)
    envelope["nextSequence"] = count + 1
    envelope["events"] = [dict(record, sequence=index+1) for index in range(count)]
    return envelope


def validate_phase(envelope, cache):
    from native_runner.cr_native_env import _validate_phase_runtime
    return _validate_phase_runtime(envelope, generation=envelope["generation"],
        state_epoch=envelope["stateEpoch"], observation_tick=envelope["observationTick"],
        validation_cache=cache)


def test_phase_cache_reuses_exact_records_and_revalidates_changed_epoch():
    from native_runner import cr_native_env as native
    cache = native._PhaseValidationCache()
    envelope = phase_envelope()
    with patch.object(native, "_validate_combat_entity_fact",
                      wraps=native._validate_combat_entity_fact) as facts:
        validate_phase(envelope, cache)
        assert facts.call_count == 6
        validate_phase(deepcopy(envelope), cache)
        assert facts.call_count == 6
        envelope["stateEpoch"] += 1
        validate_phase(envelope, cache)
        assert facts.call_count == 12
        envelope["observationTick"] -= 1
        validate_phase(envelope, cache)
        assert facts.call_count == 18
    assert len(cache.records) == 2


@pytest.mark.parametrize("field,value", [("sequence", True), ("tick", 1.0),
    ("success", 1), ("hookOffset", 0), ("outputStep", -1)])
def test_phase_cache_does_not_trust_changed_old_records(field, value):
    from native_runner.cr_native_env import _PhaseValidationCache, RunnerError
    cache = _PhaseValidationCache()
    envelope = phase_envelope()
    validate_phase(envelope, cache)
    envelope["events"][0][field] = value
    with pytest.raises(RunnerError):
        validate_phase(envelope, cache)


def test_phase_cache_keeps_only_current_transmitted_ring_and_checks_headers():
    from native_runner.cr_native_env import _PhaseValidationCache, RunnerError
    cache = _PhaseValidationCache()
    envelope = phase_envelope()
    validate_phase(envelope, cache)
    envelope["transmitFromSequence"] = 2
    envelope["events"] = envelope["events"][1:]
    validate_phase(envelope, cache)
    assert set(cache.records) == {2}
    envelope["complete"] = True
    envelope["rejectedCount"] = 1
    with pytest.raises(RunnerError, match="complete marker"):
        validate_phase(envelope, cache)
    cache.clear()
    assert cache.identity is None and cache.records == {}


def test_phase_cache_validates_new_records_and_rejects_cursor_gaps():
    from native_runner.cr_native_env import _PhaseValidationCache, _validate_phase_runtime, RunnerError
    cache = _PhaseValidationCache()
    envelope = phase_envelope(1)
    validate_phase(envelope, cache)
    envelope["nextSequence"] = 3
    envelope["events"].append(dict(envelope["events"][0], sequence=2, success=0))
    with pytest.raises(RunnerError, match="success"):
        validate_phase(envelope, cache)
    assert set(cache.records) == {1}
    envelope["events"][1]["success"] = True
    validate_phase(envelope, cache)
    assert set(cache.records) == {1, 2}
    envelope["transmitFromSequence"] = 3
    envelope["events"] = []
    with pytest.raises(RunnerError, match="skipped"):
        _validate_phase_runtime(envelope, generation=envelope["generation"],
            state_epoch=envelope["stateEpoch"], observation_tick=envelope["observationTick"],
            minimum_sequence=2, validation_cache=cache)
