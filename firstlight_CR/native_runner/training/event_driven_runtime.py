"""Correctness-first Python event loop for EVENT_DRIVEN_SPEC P2.

This prototype intentionally advances one native tick at a time. It validates
plan cancellation and event ordering before the P4 native step-until-event
optimization replaces the scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .event_config import EventPolicyConfigV1
from .event_action_space import native_hand_slots
from .event_scheduling import complete_event_commands
from .event_driven import EventActionTargetV1, EventTriggerV1, cancel_pending_plan
from ..contracts import ActionV1


@dataclass(frozen=True, slots=True)
class PendingEventPlanV1:
    target: EventActionTargetV1
    scheduled_tick: int
    created_tick: int


class EventDrivenRuntimeV1:
    """Wrap a BattleEnv-like environment with event-boundary stepping."""

    def __init__(self, environment: Any, *, owner: int, config: EventPolicyConfigV1 | None = None) -> None:
        if owner not in (0, 1):
            raise ValueError("event runtime owner must be 0 or 1")
        self.environment = environment
        self.owner = owner
        self.config = config or EventPolicyConfigV1()
        self.pending_plan: PendingEventPlanV1 | None = None
        self.last_event_tick: int | None = None
        self.last_event_ids: set[str] = set()
        self.cancelled_plans = 0
        self.executed_plans = 0
        self.wait_wakes = 0

    def _current_tick(self) -> int:
        return int(self.environment.observe(owner=self.owner, policy_features_only=True).tick)

    def _deployment_events(self) -> tuple[EventTriggerV1, ...]:
        events = self.environment.deployment_timing_events(0) + self.environment.deployment_timing_events(1)
        result: list[EventTriggerV1] = []
        for item in events:
            event_id = str(item.get("nativeEventId", ""))
            if event_id in self.last_event_ids:
                continue
            result.append(
                EventTriggerV1(
                    tick=int(item["tick"]),
                    event_type="deployment",
                    owner=int(item["owner"]) if item.get("owner") is not None else None,
                    card_id=int(item["cardId"]) if item.get("cardId") is not None else None,
                    entity_id=(int(item["nativeObjectId"]) if item.get("nativeObjectId") is not None else None),
                    data={"native_event_id": event_id},
                )
            )
            self.last_event_ids.add(event_id)
        return tuple(sorted(result, key=lambda item: (item.tick, item.owner or -1)))

    def _wait_actions(self) -> Mapping[int, tuple[ActionV1, ...]]:
        return complete_event_commands({})

    def install_plan(self, target: EventActionTargetV1) -> PendingEventPlanV1:
        """Install/replace one plan at the current event boundary."""

        if target.owner != self.owner:
            raise ValueError("event plan owner does not match runtime owner")
        now = self._current_tick()
        if target.wait:
            self.pending_plan = None
            self.wait_wakes += 1
            return PendingEventPlanV1(target=target, scheduled_tick=now, created_tick=now)
        plan = PendingEventPlanV1(
            target=target,
            scheduled_tick=now + (target.delay_ms + 49) // 50,
            created_tick=now,
        )
        self.pending_plan = plan
        return plan

    def _plan_action(self, plan: PendingEventPlanV1) -> ActionV1:
        assert plan.target.card_id is not None and plan.target.target_cell is not None
        # Event P2 uses a hand-slot lookup at execution time; card identity is
        # retained as metadata so the environment's fair action validator binds
        # the currently available hand slot.
        observation = self.environment.observe(owner=self.owner)
        player = next(item for item in observation.players if item.owner == self.owner)
        try:
            hand_index = next(slot for slot, card in native_hand_slots(player) if card == plan.target.card_id)
        except StopIteration:
            raise RuntimeError("planned card is no longer in the actor hand")
        return ActionV1.play(
            self.owner,
            hand_index,
            plan.target.target_cell,
            card_id=plan.target.card_id,
            execute_offset_ticks=1,
            action_id=plan.target.action_id,
        )

    def advance_until_event(self) -> tuple[dict[int, Any], tuple[EventTriggerV1, ...], bool]:
        """Advance one tick at a time until event, plan execution, or terminal.

        This is the correctness prototype. P4 replaces the loop with one
        native step-until-event call after these cancellation semantics pass
        against recorded traces.
        """

        waited = 0
        while True:
            now = self._current_tick()
            commands = self._wait_actions()
            submitted: PendingEventPlanV1 | None = None
            if self.pending_plan is not None and now >= self.pending_plan.scheduled_tick:
                try:
                    commands = dict(commands)
                    submitted = self.pending_plan
                    commands[self.owner] = (self._plan_action(submitted),)
                    self.pending_plan = None
                except RuntimeError:
                    self.pending_plan = None
            observations, _rewards, terms, truncs, _infos = self.environment.step(
                commands, record_trace=False, policy_features_only=True
            )
            events = self._deployment_events()
            terminal = bool(terms["__all__"]) or bool(truncs["__all__"])
            if submitted is not None:
                # Count only after the post-step public deployment ring confirms
                # the planned card. A competing event in the same step wins
                # cancellation unless it is the submitted card itself.
                confirmed = any(
                    event.owner == self.owner
                    and event.card_id == submitted.target.card_id
                    and event.tick >= submitted.scheduled_tick
                    for event in events
                )
                if confirmed:
                    self.executed_plans += 1
                elif events:
                    self.cancelled_plans += 1
                else:
                    self.cancelled_plans += 1
            if events:
                trigger = events[-1]
                self.last_event_tick = trigger.tick
                if self.pending_plan is not None and trigger.tick < self.pending_plan.scheduled_tick:
                    self.cancelled_plans += 1
                    self.pending_plan = None
                return observations, events, terminal
            if terminal:
                return observations, events, terminal
            waited += 1
            if waited >= self.config.max_wait_ticks:
                # Safety wake: do not spin forever when the engine produces no
                # public event (e.g. both actors remain passive).
                self.wait_wakes += 1
                return observations, (
                    EventTriggerV1(tick=self._current_tick(), event_type="timeout"),
                ), False

    def step_event(self, target: EventActionTargetV1) -> tuple[dict[int, Any], tuple[EventTriggerV1, ...], bool]:
        """Install an event-boundary prediction and scan to its next wake."""

        if self.last_event_tick is not None and target.source_tick != self.last_event_tick:
            raise ValueError("event target source tick is not the latest event boundary")
        self.install_plan(target)
        return self.advance_until_event()
