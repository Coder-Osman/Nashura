from types import SimpleNamespace

import pytest

from native_runner.battle_env import BattleEnvError
from native_runner.training.fair_final.native import FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction


class TickEnvironment:
    def __init__(self, *, events=(), rejected=(), terminal_tick=None):
        self.current_tick = 100
        self.events = set(events)
        self.rejected = set(rejected)
        self.terminal_tick = terminal_tick
        self.executions = []
        self.observations = []

    def observe_dual(self, **kwargs):
        self.observations.append((self.current_tick, tuple(self.executions)))
        return {owner: SimpleNamespace(tick=self.current_tick, owner=owner,
            executed=tuple(self.executions)) for owner in (0, 1)}

    def advance_reservations(self, plans):
        self.current_tick += 1
        results = {}
        for plan in plans:
            assert plan.due_tick == self.current_tick
            results[plan.identity] = "rejected" if plan.owner in self.rejected else "executed"
            if results[plan.identity] == "executed":
                self.executions.append((plan.owner, self.current_tick))
        done = self.current_tick == self.terminal_tick
        return results, ({}, {}, {"__all__": done}, {"__all__": False}, {})

    def action_outcomes(self):
        if self.current_tick in self.events or any(tick == self.current_tick for owner, tick in self.executions):
            return (SimpleNamespace(event_type="action_executed", data={}),)
        return ()


def commit(runtime, delay=1):
    runtime.commit_simultaneous({0: FullAction(11, 0, delay), 1: FullAction()}, legal=lambda *args: True)


def test_native_runtime_freezes_only_after_due_execution_and_preemption():
    environment = TickEnvironment(events=(102,))
    runtime = FinalDecisionRuntime(environment)
    commit(runtime)
    runtime.advance()
    assert runtime.freeze_due_information() == {}
    runtime.advance()
    frozen = runtime.freeze_due_information()
    assert set(frozen) == {0, 1}
    assert all(observation.executed == ((0, 102),) for observation in frozen.values())
    assert not runtime.clock.pending
    assert [item["reason"] for item in runtime.clock.evidence] == ["executed", "preempted"]


def test_preemption_does_not_leave_a_native_command_queued():
    environment = TickEnvironment(events=(101,))
    runtime = FinalDecisionRuntime(environment)
    commit(runtime)
    runtime.advance()
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    runtime.advance()
    assert environment.executions == []
    assert [item["reason"] for item in runtime.clock.evidence] == ["preempted", "preempted"]


def test_rejection_wakes_only_its_owner_and_terminal_opens_no_new_decision():
    environment = TickEnvironment(rejected=(0,), terminal_tick=103)
    runtime = FinalDecisionRuntime(environment)
    commit(runtime)
    runtime.advance()
    runtime.advance()
    assert set(runtime.freeze_due_information()) == {0}
    assert runtime.clock.evidence[-1]["reason"] == "rejected"
    runtime.commit_simultaneous({0: FullAction()}, legal=lambda *args: True)
    runtime.advance()
    assert runtime.ended and not runtime.truncated
    assert not runtime.freeze_due_information()
    assert not runtime.clock.pending
    with pytest.raises(BattleEnvError):
        runtime.advance()


def test_failed_native_step_quarantines_runtime():
    environment = TickEnvironment()
    runtime = FinalDecisionRuntime(environment)
    commit(runtime)

    def broken(plans):
        raise RuntimeError("partial queue receipt")

    environment.advance_reservations = broken
    with pytest.raises(RuntimeError):
        runtime.advance()
    with pytest.raises(BattleEnvError):
        runtime.advance()
    with pytest.raises(BattleEnvError):
        runtime.freeze_due_information()


def test_same_tick_opponent_choice_does_not_change_already_frozen_information():
    environment = TickEnvironment()
    runtime = FinalDecisionRuntime(environment)
    first = runtime.freeze_due_information()
    first[0].executed = ((1, 999),)
    assert runtime.freeze_due_information()[0].executed == ()
    with pytest.raises(ValueError):
        runtime.commit_simultaneous({0: FullAction()}, legal=lambda *args: True)
    assert runtime.clock.pending == {} and runtime.clock.ready == {0, 1}


def test_wait_timeout_has_no_private_opponent_wakeup():
    environment = TickEnvironment()
    runtime = FinalDecisionRuntime(environment)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    for offset in range(59):
        runtime.advance()
        assert runtime.freeze_due_information() == {}
    runtime.advance()
    assert runtime.clock.tick == 160
    assert [entry['reason'] for entry in runtime.clock.evidence] == ['wait_expired', 'wait_expired']
