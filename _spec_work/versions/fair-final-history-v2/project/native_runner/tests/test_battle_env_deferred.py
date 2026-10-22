from unittest.mock import patch

import pytest

from native_runner.tests.test_battle_env import _make_env, _reset, FakeNativeEnv
from native_runner.contracts import ActionV1


def test_deferred_observations_preserve_transitions_rewards_events_and_masks():
    eager, _ = _make_env(FakeNativeEnv(initial_entity=True))
    lazy, _ = _make_env(FakeNativeEnv(initial_entity=True))
    _reset(eager)
    _reset(lazy)
    profile = {}
    for _ in range(4):
        actions = {owner: ActionV1.wait(owner, ticks=2) for owner in (0, 1)}
        expected = eager.step(actions, record_trace=False, policy_features_only=True)
        with patch.object(lazy, "observe_dual", side_effect=AssertionError("unexpected materialization")):
            actual = lazy.step(actions, record_trace=False, policy_features_only=True,
                               defer_observations=True, timing_profile=profile)
        assert actual[0] == {}
        assert actual[1:] == expected[1:]
        assert lazy.current_tick == expected[0][0].tick
    refreshed = lazy.observe_dual(policy_features_only=True)
    for owner in (0, 1):
        for name in ("tick", "players", "towers", "entities", "action_mask", "terminal", "truncated"):
            assert getattr(refreshed[owner], name) == getattr(expected[0][owner], name), name
        # Generated request UUIDs differ across environments; public game events must agree.
        public = lambda obs: [(e.tick, e.event_type, e.owner, e.card_id) for e in obs.events]
        assert public(refreshed[owner]) == public(expected[0][owner])
    assert profile["step_transition_seconds"] > 0
    assert profile["step_native_bind_seconds"] > 0


def test_plain_wait_skips_masks_but_retains_owner_validation():
    env, _ = _make_env()
    _reset(env)
    with patch.object(env, "action_mask", side_effect=AssertionError("WAIT requested mask")):
        env._validate_compound_actions(0, (ActionV1.wait(0, ticks=2),), env._raw)
    with pytest.raises(Exception, match="owner"):
        env._validate_compound_actions(0, (ActionV1.wait(1, ticks=2),), env._raw)


def test_deferred_api_requires_explicit_policy_mode():
    env, _ = _make_env()
    _reset(env)
    with pytest.raises(ValueError, match="deferred"):
        env.step({}, defer_observations=True)
