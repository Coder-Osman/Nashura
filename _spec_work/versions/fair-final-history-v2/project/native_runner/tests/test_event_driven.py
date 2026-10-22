from __future__ import annotations

import unittest

from native_runner.contracts import ActionKind, ActionV1
from native_runner.training.event_driven import (
    EventActionTargetV1,
    EventDecisionV1,
    EventTrajectoryV1,
    EventTriggerV1,
    build_expert_event_trajectory,
    delay_ms_to_ticks,
    ticks_to_delay_ms,
)
from native_runner.training.v4.expert import TimedExpertActionV4


class EventDrivenContractTests(unittest.TestCase):
    def test_delay_quantization_is_ceil_and_round_trip(self) -> None:
        self.assertEqual(delay_ms_to_ticks(0), 0)
        self.assertEqual(delay_ms_to_ticks(1), 1)
        self.assertEqual(delay_ms_to_ticks(50), 1)
        self.assertEqual(delay_ms_to_ticks(51), 2)
        self.assertEqual(ticks_to_delay_ms(7), 350)

    def test_wait_target_cannot_carry_card_or_position(self) -> None:
        with self.assertRaises(ValueError):
            EventActionTargetV1(owner=0, wait=True, card_id=26000021, source_tick=90)
        with self.assertRaises(ValueError):
            EventActionTargetV1(owner=0, wait=True, target_cell=(1, 1), source_tick=90)

    def test_act_target_requires_card_and_grid(self) -> None:
        with self.assertRaises(ValueError):
            EventActionTargetV1(owner=0, wait=False, card_id=26000021, source_tick=90)
        with self.assertRaises(ValueError):
            EventActionTargetV1(owner=0, wait=False, card_id=26000021, target_cell=(18, 1), source_tick=90)

    def test_event_trajectory_is_monotonic_and_waits_until_next_actor_action(self) -> None:
        actions = (
            TimedExpertActionV4(
                source_tick=120,
                action=ActionV1.play(0, 0, (8, 20), card_id=26000021),
                source_index=0,
            ),
            TimedExpertActionV4(
                source_tick=170,
                action=ActionV1.play(1, 0, (8, 10), card_id=26000004),
                source_index=1,
            ),
            TimedExpertActionV4(
                source_tick=300,
                action=ActionV1.play(0, 1, (8, 18), card_id=26000030),
                source_index=2,
            ),
        )
        trajectory = build_expert_event_trajectory(
            actions, owner=0, match_id="m0", first_event_tick=90, terminal_tick=500
        )
        self.assertIsInstance(trajectory, EventTrajectoryV1)
        self.assertEqual(trajectory.owner, 0)
        self.assertEqual(tuple(row.observation_tick for row in trajectory.decisions), (90, 120, 170, 300))
        self.assertEqual(trajectory.decisions[0].target.wait, False)
        self.assertEqual(trajectory.decisions[0].target.card_id, 26000021)
        self.assertEqual(trajectory.decisions[0].target.delay_ms, 1500)
        self.assertEqual(trajectory.decisions[1].target.wait, False)
        self.assertEqual(trajectory.decisions[1].target.card_id, 26000021)
        self.assertEqual(trajectory.decisions[1].target.target_cell, (8, 20))
        # The actor action at tick 300 is predicted from the opponent event;
        # no private opponent payload is copied.
        self.assertEqual(trajectory.decisions[2].target.wait, False)
        self.assertEqual(trajectory.decisions[2].target.card_id, 26000030)
        self.assertEqual(trajectory.decisions[2].target.delay_ms, 6000)
        self.assertEqual(trajectory.decisions[3].target.card_id, 26000030)
        self.assertEqual(trajectory.action_count, 4)

    def test_cancelled_plan_is_not_executed(self) -> None:
        target = EventActionTargetV1(
            owner=0, wait=False, card_id=26000021, target_cell=(8, 20), delay_ms=1000, source_tick=90
        )
        row = EventDecisionV1(
            schema="event-driven-decision.v1",
            trigger=EventTriggerV1(tick=90, event_type="deployment"),
            observation_tick=90,
            target=target,
            next_trigger_tick=100,
            plan_cancelled=True,
            cancellation_tick=100,
        )
        self.assertFalse(row.executed)
        self.assertIsNone(row.execution_delay_ticks)

    def test_terminal_decision_has_no_successor(self) -> None:
        target = EventActionTargetV1(owner=0, wait=True, source_tick=90)
        row = EventDecisionV1(
            schema="event-driven-decision.v1",
            trigger=EventTriggerV1(tick=90, event_type="terminal"),
            observation_tick=90,
            target=target,
            next_trigger_tick=None,
            terminal=True,
        )
        self.assertEqual(row.delta_ticks, 0)
        with self.assertRaises(ValueError):
            EventDecisionV1(
                schema="event-driven-decision.v1",
                trigger=EventTriggerV1(tick=90, event_type="terminal"),
                observation_tick=90,
                target=target,
                next_trigger_tick=100,
                terminal=True,
            )


if __name__ == "__main__":
    unittest.main()
