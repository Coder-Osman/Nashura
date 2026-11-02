from types import SimpleNamespace

import pytest

from native_runner.battle_env import BattleEnvError
from native_runner.training.fair_final.native import FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction


class TickEnvironment:
    def __init__(self, *, events=(), rejected=(), terminal_tick=None, short_hands=(), refill_tick=None,
            supports_event_batched_advance=False):
        self.current_tick = 100
        self.events = set(events)
        self.rejected = set(rejected)
        self.terminal_tick = terminal_tick
        self.refill_tick = refill_tick
        self.supports_event_batched_advance = supports_event_batched_advance
        self.raw_observation = {"players": [
            {"owner": owner, "hand": [
                {"handIndex": slot, "deckSlot": slot, "cardId": owner * 100 + slot + 1}
                for slot in range(4) if (owner, slot) not in set(short_hands)]}
            for owner in (0, 1)]}
        self.executions = []
        self.observations = []
        self.advance_calls = []

    def observe_dual(self, **kwargs):
        self.observations.append((self.current_tick, tuple(self.executions)))
        return {owner: SimpleNamespace(tick=self.current_tick, owner=owner,
            executed=tuple(self.executions)) for owner in (0, 1)}

    def advance_reservations(self, plans, *, ticks=1):
        self.advance_calls.append((ticks, tuple(plan.identity for plan in plans)))
        requested_tick = self.current_tick + ticks
        events = [tick for tick in self.events if self.current_tick < tick <= requested_tick]
        terminal = self.terminal_tick if self.terminal_tick is not None and self.current_tick < self.terminal_tick <= requested_tick else None
        stop_ticks = events + ([] if terminal is None else [terminal])
        self.current_tick = min(stop_ticks) if stop_ticks else requested_tick
        if self.current_tick == self.refill_tick:
            for player in self.raw_observation["players"]:
                if player["owner"] == 0 and len(player["hand"]) < 4:
                    slot = next(slot for slot in range(4)
                        if slot not in {item["handIndex"] for item in player["hand"]})
                    player["hand"].append({"handIndex": slot, "deckSlot": slot + 4,
                        "cardId": 201})
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


def test_wait_deadline_uses_one_event_batched_observation():
    environment = TickEnvironment(supports_event_batched_advance=True)
    runtime = FinalDecisionRuntime(environment)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    runtime.advance()
    assert runtime.clock.tick == 160
    assert environment.advance_calls == [(60, ())]
    assert runtime.advance_profile == dict(native_calls=1, actual_native_ticks=60,
        host_observations_avoided=59, refill_poll_ticks=0, early_event_wakes=0)
    assert [entry['reason'] for entry in runtime.clock.evidence] == ['wait_expired', 'wait_expired']


def test_public_deployment_stops_a_batched_advance_and_preempts_pending_actions():
    environment = TickEnvironment(events=(109,), supports_event_batched_advance=True)
    runtime = FinalDecisionRuntime(environment, wait_ticks=20)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)

    runtime.advance()

    assert runtime.clock.tick == 109
    assert set(runtime.freeze_due_information()) == {0, 1}
    assert environment.advance_calls == [(20, ())]
    assert runtime.advance_profile["early_event_wakes"] == 1
    assert [item["reason"] for item in runtime.clock.evidence] == ["preempted", "preempted"]


def test_short_hand_keeps_exact_tick_refill_wakeup():
    environment = TickEnvironment(short_hands=((0, 0),), refill_tick=103,
        supports_event_batched_advance=True)
    runtime = FinalDecisionRuntime(environment, wait_ticks=20, wake_refills=True)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)

    runtime.advance()
    assert runtime.freeze_due_information() == {}
    runtime.advance()
    assert runtime.freeze_due_information() == {}
    runtime.advance()

    assert runtime.clock.tick == 103
    assert set(runtime.freeze_due_information()) == {0}
    assert environment.advance_calls == [(1, ()), (1, ()), (1, ())]
    assert runtime.advance_profile["refill_poll_ticks"] == 3
    assert runtime.clock.evidence[-1]["reason"] == "preempted"


def test_sspi_wait_uses_one_second_and_snapshot_keeps_its_clock():
    from native_runner.training.fair_final.timing import DecisionClock

    clock = DecisionClock(wait_ticks=20)
    clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {},
        freeze=lambda owner, tick: (owner, tick))
    clock.commit({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    assert [clock.pending[owner].due_tick for owner in (0, 1)] == [120, 120]
    assert DecisionClock.restore(clock.capture()).wait_ticks == 20
    mismatched = clock.capture()
    mismatched["pending"][0]["wait_ticks"] = 60
    with pytest.raises(ValueError, match="invalid restored"):
        DecisionClock.restore(mismatched)
