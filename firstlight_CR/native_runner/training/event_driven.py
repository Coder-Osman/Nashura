"""Event-driven action targets and trajectory utilities.

The event-driven policy makes one decision after every public deployment event
(or a safety wake), predicting the next card, target cell, and a delay. This
module contains the pure data contract and replay conversion; native stepping
is deliberately kept out until the labels and invariants are validated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .v4.expert import TimedExpertActionV4
from .event_config import EventPolicyConfigV1

EVENT_SCHEMA = "event-driven-decision.v1"
EVENT_TYPES = frozenset({"deployment", "elixir_threshold", "timeout", "terminal"})
DEFAULT_MAX_WAIT_TICKS = 300  # 15 seconds at the native 20 Hz clock
DEFAULT_ELIXIR_WAKE_RAW = 95_000


@dataclass(frozen=True, slots=True)
class EventTriggerV1:
    """The public event that caused one event-policy inference."""

    tick: int
    event_type: str
    owner: int | None = None
    card_id: int | None = None
    entity_id: int | None = None
    position: tuple[float, float] | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tick < 0 or self.event_type not in EVENT_TYPES:
            raise ValueError("invalid event trigger tick/type")
        if self.owner not in (None, 0, 1):
            raise ValueError("event trigger owner must be 0, 1, or null")
        if self.card_id is not None and self.card_id < 0:
            raise ValueError("event trigger card id must be non-negative")
        if self.entity_id is not None and self.entity_id < 0:
            raise ValueError("event trigger entity id must be non-negative")
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True, slots=True)
class EventActionTargetV1:
    """One event-policy output: next action or explicit wait."""

    owner: int
    wait: bool
    card_id: int | None = None
    target_cell: tuple[int, int] | None = None
    delay_ms: int = 0
    source_tick: int = 0
    action_id: str = ""

    def __post_init__(self) -> None:
        if self.owner not in (0, 1):
            raise ValueError("event action owner must be 0 or 1")
        if self.delay_ms < 0 or self.delay_ms > 60_000:
            raise ValueError("event action delay must be in 0..60000 ms")
        if self.source_tick < 0:
            raise ValueError("event action source tick must be non-negative")
        if self.wait:
            if self.card_id is not None or self.target_cell is not None:
                raise ValueError("wait target cannot carry card or target")
        else:
            if self.card_id is None:
                raise ValueError("ACT target requires card id")
            if self.target_cell is None:
                raise ValueError("ACT target requires target cell")
            x, y = self.target_cell
            if not 0 <= x < 18 or not 0 <= y < 32:
                raise ValueError("event target cell is outside the 18x32 grid")


@dataclass(frozen=True, slots=True)
class EventDecisionV1:
    """One sparse event-policy training row."""

    schema: str
    trigger: EventTriggerV1
    observation_tick: int
    target: EventActionTargetV1
    next_trigger_tick: int | None
    terminal: bool = False
    plan_cancelled: bool = False
    cancellation_tick: int | None = None

    def __post_init__(self) -> None:
        if self.schema != EVENT_SCHEMA:
            raise ValueError("unsupported event decision schema")
        if self.observation_tick != self.trigger.tick:
            raise ValueError("event observation and trigger ticks must agree")
        if self.next_trigger_tick is not None and self.next_trigger_tick < self.observation_tick:
            raise ValueError("next event precedes current event")
        if self.terminal and self.next_trigger_tick is not None:
            raise ValueError("terminal event cannot have a next trigger")
        if self.plan_cancelled:
            if self.target.wait:
                raise ValueError("only an ACT plan can be cancelled")
            if self.cancellation_tick is None or self.cancellation_tick < self.observation_tick:
                raise ValueError("cancelled plan needs a valid cancellation tick")
        elif self.cancellation_tick is not None:
            raise ValueError("non-cancelled plan cannot carry cancellation tick")

    @property
    def delta_ticks(self) -> int:
        if self.next_trigger_tick is None:
            return 0
        return self.next_trigger_tick - self.observation_tick

    @property
    def discount_exponent(self) -> int:
        """Actual semi-Markov discount duration for this event row."""

        return self.delta_ticks

    @property
    def executed(self) -> bool:
        """Whether this row's ACT plan reached native execution."""

        return not self.target.wait and not self.plan_cancelled

    @property
    def execution_delay_ticks(self) -> int | None:
        """Delay from this trigger to execution, absent for WAIT/cancelled."""

        if not self.executed:
            return None
        return delay_ms_to_ticks(self.target.delay_ms)


@dataclass(frozen=True, slots=True)
class EventTrajectoryV1:
    """One actor's sparse event sequence for a single match."""

    owner: int
    decisions: tuple[EventDecisionV1, ...]
    match_id: str = ""

    def __post_init__(self) -> None:
        if self.owner not in (0, 1):
            raise ValueError("trajectory owner must be 0 or 1")
        ticks = tuple(row.observation_tick for row in self.decisions)
        if ticks != tuple(sorted(ticks)) or len(set(ticks)) != len(ticks):
            raise ValueError("event decisions must have strictly increasing ticks")
        if any(row.target.owner != self.owner for row in self.decisions):
            raise ValueError("trajectory target owner changed")

    @property
    def event_count(self) -> int:
        return len(self.decisions)

    @property
    def action_count(self) -> int:
        return sum(not row.target.wait for row in self.decisions)

    @property
    def action_fraction(self) -> float:
        return self.action_count / max(1, self.event_count)


def delay_ms_to_ticks(delay_ms: int) -> int:
    """Ceil an event delay to native ticks; actions cannot execute retroactively."""

    if delay_ms < 0:
        raise ValueError("delay must be non-negative")
    return (int(delay_ms) + 49) // 50


def ticks_to_delay_ms(ticks: int) -> int:
    if ticks < 0:
        raise ValueError("ticks must be non-negative")
    return int(ticks) * 50


def _target_from_expert(
    item: TimedExpertActionV4, *, trigger_tick: int, config: EventPolicyConfigV1
) -> EventActionTargetV1:
    action = item.action
    delay_ms = config.delay_offset_ms[config.delay_to_bin(ticks_to_delay_ms(item.source_tick - trigger_tick))]
    if action.kind.value == "wait":
        return EventActionTargetV1(
            owner=action.owner,
            wait=True,
            delay_ms=delay_ms,
            source_tick=trigger_tick,
            action_id=action.action_id,
        )
    if action.kind.value != "play_card":
        # Ability activations remain actions but have no card/placement label in
        # the first event schema; represent them as a non-wait target with the
        # source card identity when available in metadata.
        card_id = action.card_id or action.metadata.get("card_id")
    else:
        card_id = action.card_id
    target = action.target_grid
    if card_id is None or target is None:
        raise ValueError("expert action lacks card id or grid target for event labels")
    return EventActionTargetV1(
        owner=action.owner,
        wait=False,
        card_id=int(card_id),
        target_cell=(int(target[0]), int(target[1])),
        delay_ms=delay_ms,
        source_tick=trigger_tick,
        action_id=action.action_id,
    )


def build_expert_event_trajectory(
    actions: Sequence[TimedExpertActionV4],
    *,
    owner: int,
    match_id: str = "",
    first_event_tick: int = 90,
    terminal_tick: int | None = None,
    config: EventPolicyConfigV1 | None = None,
) -> EventTrajectoryV1:
    """Convert one owner's timestamped expert actions into sparse rows.

    The first row is an explicit wake at ``first_event_tick``. Subsequent rows
    occur at each public expert deployment in chronological order. At every row
    the target is the *next* owner action after that trigger; if the next owner
    action has not yet occurred, the row is WAIT and its next trigger is that
    action's tick or the next opponent deployment, whichever is earlier. This
    creates labels without leaking future opponent private state.
    """

    if owner not in (0, 1):
        raise ValueError("owner must be 0 or 1")
    actual_config = config or EventPolicyConfigV1()
    ordered = tuple(sorted(actions, key=lambda item: (item.source_tick, item.source_index, item.owner)))
    owner_actions = tuple(item for item in ordered if item.owner == owner)
    public_events = tuple(item for item in ordered if item.source_tick >= first_event_tick)
    if not public_events and not owner_actions:
        return EventTrajectoryV1(owner=owner, decisions=(), match_id=match_id)

    rows: list[EventDecisionV1] = []
    trigger_ticks = [first_event_tick]
    trigger_ticks.extend(item.source_tick for item in public_events if item.source_tick != first_event_tick)
    trigger_ticks = sorted(set(trigger_ticks))
    for trigger_index, trigger_tick in enumerate(trigger_ticks):
        next_owner = next((item for item in owner_actions if item.source_tick >= trigger_tick), None)
        if next_owner is None:
            next_tick = terminal_tick
            target = EventActionTargetV1(owner=owner, wait=True, source_tick=trigger_tick)
        else:
            # The event policy predicts the next owner action even when it is
            # scheduled after the current trigger. WAIT is reserved for a
            # state with no remaining owner action before terminal; otherwise
            # the delay head would receive only zero/WAIT labels.
            target = _target_from_expert(next_owner, trigger_tick=trigger_tick, config=actual_config)
            next_tick = next(
                (item.source_tick for item in public_events if item.source_tick > trigger_tick),
                terminal_tick,
            )
            if next_tick is None:
                next_tick = next_owner.source_tick
            next_tick = min(next_tick, next_owner.source_tick) if next_owner.source_tick > trigger_tick else next_tick
            if next_owner.source_tick > trigger_tick:
                next_tick = next_owner.source_tick
            else:
                next_tick = next(
                    (item.source_tick for item in public_events if item.source_tick > trigger_tick),
                    terminal_tick,
                )
            if next_tick is None and trigger_index + 1 < len(trigger_ticks):
                next_tick = trigger_ticks[trigger_index + 1]
        if next_tick is None and trigger_index + 1 < len(trigger_ticks):
            next_tick = trigger_ticks[trigger_index + 1]
        terminal = terminal_tick is not None and next_tick == terminal_tick
        rows.append(
            EventDecisionV1(
                schema=EVENT_SCHEMA,
                trigger=EventTriggerV1(tick=trigger_tick, event_type="deployment"),
                observation_tick=trigger_tick,
                target=target,
                next_trigger_tick=None if terminal else next_tick,
                terminal=terminal,
            )
        )
    return EventTrajectoryV1(owner=owner, decisions=tuple(rows), match_id=match_id)


def cancel_pending_plan(
    decision: EventDecisionV1, *, cancellation_tick: int, replacement_trigger: EventTriggerV1
) -> EventDecisionV1:
    """Mark a scheduled ACT obsolete when a newer event arrives first."""

    if decision.target.wait or decision.plan_cancelled:
        raise ValueError("only an active ACT plan may be cancelled")
    if cancellation_tick < decision.observation_tick:
        raise ValueError("cancellation cannot precede the original decision")
    if replacement_trigger.tick != cancellation_tick:
        raise ValueError("replacement event and cancellation ticks must agree")
    return EventDecisionV1(
        schema=decision.schema,
        trigger=decision.trigger,
        observation_tick=decision.observation_tick,
        target=decision.target,
        next_trigger_tick=cancellation_tick,
        terminal=False,
        plan_cancelled=True,
        cancellation_tick=cancellation_tick,
    )


def collect_public_deployment_events(events: Iterable[Mapping[str, Any]]) -> tuple[EventTriggerV1, ...]:
    """Normalize probe/replay deployment records into public event triggers."""

    result: list[EventTriggerV1] = []
    for raw in events:
        event_type = str(raw.get("event_type", raw.get("type", "")))
        if event_type not in {"deployment", "card_deployed", "card_form_command_queued"}:
            continue
        result.append(
            EventTriggerV1(
                tick=int(raw["tick"]),
                event_type="deployment",
                owner=(None if raw.get("owner") is None else int(raw["owner"])),
                card_id=(None if raw.get("card_id", raw.get("cardId")) is None else int(raw.get("card_id", raw.get("cardId")))),
                entity_id=(None if raw.get("entity_id", raw.get("entityId")) is None else int(raw.get("entity_id", raw.get("entityId")))),
                position=raw.get("position"),
                data={"source": "public_event"},
            )
        )
    return tuple(sorted(result, key=lambda item: (item.tick, item.owner or -1, item.card_id or -1)))
