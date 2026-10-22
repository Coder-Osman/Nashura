from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from native_runner.battle_env import BattleEnvV1
from native_runner.contracts import EventV1
from native_runner.tests.test_fair_final_information import tracker
from native_runner.tests.test_training_tracking import _observation
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.visibility import event_visible_to_owner, validate_fair_view


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("scope", ["public", "own", "enemy", "oracle", "malformed"])
@pytest.mark.parametrize("combat_visible", [True, False, None])
def test_event_visibility_permissions_intersect(owner, scope, combat_visible):
    data = {"public": {}, "own": {"private_to": owner},
        "enemy": {"private_to": 1 - owner}, "oracle": {"oracle_only": True},
        "malformed": {"private_to": str(owner)}}[scope]
    combat = SimpleNamespace(visible_by_owner={str(owner): combat_visible})
    event = SimpleNamespace(data=data, combat=combat)
    expected = scope in ("public", "own") and combat_visible is True
    assert event_visible_to_owner(event, owner) == expected
    assert FinalBattleEnv._event_visible_to(event, owner) == expected
    assert tracker(owner)._event_visible_to_observer(event, owner) == expected


def test_new_runtime_closes_precedence_gap_without_changing_legacy():
    event = SimpleNamespace(data={"oracle_only": True},
        combat=SimpleNamespace(visible_by_owner={"0": True}))
    assert BattleEnvV1._event_visible_to(event, 0)
    assert not FinalBattleEnv._event_visible_to(event, 0)


@pytest.mark.parametrize("owner", [0, 1])
def test_unscoped_public_event_and_private_own_event_remain_visible(owner):
    for data in ({}, {"private_to": owner}):
        event = EventV1(tick=1, event_type="action_executed", owner=owner, card_id=101, data=data)
        assert event_visible_to_owner(event, owner)
    assert not event_visible_to_owner(event, None)


@pytest.mark.parametrize("tick", [100, 1200, 3600, 5900])
@pytest.mark.parametrize("private_field,value", [("hand", (101,)), ("cycle", (102,)),
    ("deck", (103,)), ("next_card", 104), ("elixir_exact", 7.), ("elixir_visible", 7.),
    ("metadata", {"hidden_seed": 77}), ("ability_state", {"ready": True}),
    ("evolution_state", {"cycle": 2}), ("private_state_visible", True)])
def test_private_opponent_fields_rejected_throughout_trajectory(tick, private_field, value):
    observation = _observation(tick=tick)
    enemy = SimpleNamespace(**{field.name: getattr(observation.players[1], field.name)
        for field in fields(observation.players[1])})
    setattr(enemy, private_field, value)
    poisoned = SimpleNamespace(**{field.name: getattr(observation, field.name) for field in fields(observation)})
    poisoned.players = (observation.players[0], enemy)
    with pytest.raises(ValueError, match="opponent private state"):
        validate_fair_view(poisoned, 0)
    with pytest.raises(ValueError, match="opponent private state"):
        tracker().update(poisoned)


def test_private_and_oracle_events_do_not_reveal_opponent_cards():
    result = tracker()
    for tick in (100, 1200, 3600, 5900):
        result.update(_observation(tick=tick, events=(
            EventV1(tick=tick, event_type="action_executed", owner=1, card_id=120, data={"private_to": 1}),
            EventV1(tick=tick, event_type="action_executed", owner=1, card_id=121, data={"oracle_only": True}),
            EventV1(tick=tick, event_type="action_executed", owner=1, card_id=122))))
        assert result.decks[1] == (122,)


def test_native_digest_rejected_at_model_boundary():
    observation = _observation(tick=3600)
    poisoned = SimpleNamespace(**{field.name: getattr(observation, field.name) for field in fields(observation)})
    poisoned.native_digest = "private"
    with pytest.raises(ValueError, match="native audit digest"):
        validate_fair_view(poisoned, 0)


@pytest.mark.parametrize("data,visibility", [({"oracle_only": True}, {"0": True, "1": True}),
    ({"private_to": 1}, {"0": True, "1": True}), ({}, {"0": True, "1": False})])
def test_nonpublic_deployment_never_wakes_both_owners(data, visibility):
    from native_runner.tests.test_fair_final_runtime import TickEnvironment
    from native_runner.training.fair_final.native import FinalDecisionRuntime
    from native_runner.training.fair_final.policy import FullAction

    environment = TickEnvironment()
    environment.action_outcomes = lambda: (SimpleNamespace(event_type="action_executed", data=data,
        combat=SimpleNamespace(visible_by_owner=visibility)),)
    runtime = FinalDecisionRuntime(environment)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    runtime.advance()
    assert runtime.clock.ready == set() and set(runtime.clock.pending) == {0, 1}


@pytest.mark.parametrize("owner", [0, 1])
def test_enemy_private_flood_does_not_evict_public_history_or_change_summary(owner):
    from collections import deque
    from copy import deepcopy

    environment = object.__new__(FinalBattleEnv)
    environment.event_limit = 3
    environment.event_window_ticks = 20
    environment._events = deque(maxlen=3)
    environment._event_archive = {}
    environment._step_action_outcomes = []
    environment._owner_events = {seat: deque(maxlen=3) for seat in (0, 1)}
    environment._owner_event_archive = {seat: {} for seat in (0, 1)}
    for tick in range(1, 6):
        environment._append_event(EventV1(tick=tick, event_type="action_executed", owner=1-owner, card_id=101))
    before = environment._events_for(owner, 5)
    summary = environment._event_summary_for(owner, 5)
    snapshot = deepcopy(vars(environment))
    for index in range(30):
        environment._append_event(EventV1(tick=5, event_type="action_executed", owner=1-owner, card_id=102,
            data={"private_to": 1-owner} if index % 2 else {"oracle_only": True}))
    assert environment._events_for(owner, 5) == before
    assert environment._event_summary_for(owner, 5) == summary
    assert len(environment._owner_events[owner]) == 3
    assert all(not event_visible_to_owner(event, owner) for event in environment._events)
    vars(environment).update(deepcopy(snapshot))
    assert environment._events_for(owner, 5) == before
    assert environment._event_summary_for(owner, 5) == summary
    environment._append_event(EventV1(tick=6, event_type="action_executed", owner=owner, card_id=103,
        data={"private_to": owner}))
    assert len(environment._owner_events[owner]) == 3
    assert environment._events_for(owner, 6)[-1].card_id == 103
