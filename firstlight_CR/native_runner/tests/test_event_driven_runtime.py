from __future__ import annotations

import unittest
from types import SimpleNamespace

from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_driven import EventActionTargetV1, EventTriggerV1
from native_runner.training.event_driven_runtime import EventDrivenRuntimeV1


class FakeEventEnv:
    def __init__(self, events=()):
        self.tick = 0
        self.events = list(events)
        self.steps = []

    def observe(self, **_kwargs):
        return SimpleNamespace(tick=self.tick)

    def deployment_timing_events(self, owner):
        return [event for event in self.events if event["owner"] == owner and event["tick"] <= self.tick]

    def step(self, actions, **_kwargs):
        self.tick += 1
        self.steps.append(actions)
        return ({}, {0: 0.0, 1: 0.0}, {"__all__": False}, {"__all__": False}, {})


class EventDrivenRuntimeTests(unittest.TestCase):
    def test_delayed_play_uses_current_sparse_native_slot(self):
        env = FakeEventEnv()
        player = SimpleNamespace(owner=1, hand=(28000007, 27000009, 26000015), metadata={
            "hand_slot_by_card": {"28000007": 1, "27000009": 2, "26000015": 3}})
        env.observe = lambda **_: SimpleNamespace(tick=1105, players=(player,))
        runtime = EventDrivenRuntimeV1(env, owner=1)
        plan = runtime.install_plan(EventActionTargetV1(
            owner=1, wait=False, card_id=26000015, target_cell=(8, 20), delay_ms=250))
        action = runtime._plan_action(plan)
        self.assertEqual(action.hand_slot, 3)
        self.assertEqual(action.card_id, 26000015)

    def test_timeout_wake_prevents_infinite_wait(self):
        env = FakeEventEnv()
        runtime = EventDrivenRuntimeV1(env, owner=0, config=EventPolicyConfigV1(max_wait_ticks=3))
        _observations, events, terminal = runtime.advance_until_event()
        self.assertFalse(terminal)
        self.assertEqual(events[0].event_type, "timeout")
        self.assertEqual(runtime.wait_wakes, 1)
        self.assertEqual(env.tick, 3)

    def test_new_event_cancels_plan_before_execution(self):
        env = FakeEventEnv(events=({"owner": 1, "tick": 1, "cardId": 26000004, "nativeEventId": "1"},))
        runtime = EventDrivenRuntimeV1(env, owner=0, config=EventPolicyConfigV1(max_wait_ticks=5))
        runtime.last_event_tick = 0
        plan = EventActionTargetV1(owner=0, wait=False, card_id=26000021, target_cell=(8, 20), delay_ms=250)
        runtime.install_plan(plan)
        _observations, events, _terminal = runtime.advance_until_event()
        self.assertEqual(events[0].event_type, "deployment")
        self.assertEqual(runtime.cancelled_plans, 1)
        self.assertEqual(runtime.executed_plans, 0)
        self.assertIsNone(runtime.pending_plan)

    def test_wait_plan_is_not_counted_as_executed(self):
        env = FakeEventEnv(events=({"owner": 1, "tick": 1, "cardId": 26000004, "nativeEventId": "1"},))
        runtime = EventDrivenRuntimeV1(env, owner=0, config=EventPolicyConfigV1(max_wait_ticks=5))
        runtime.last_event_tick = 0
        runtime.install_plan(EventActionTargetV1(owner=0, wait=True, source_tick=0))
        _observations, events, _terminal = runtime.advance_until_event()
        self.assertEqual(events[0].event_type, "deployment")
        self.assertEqual(runtime.executed_plans, 0)
        self.assertEqual(runtime.wait_wakes, 1)


if __name__ == "__main__":
    unittest.main()
