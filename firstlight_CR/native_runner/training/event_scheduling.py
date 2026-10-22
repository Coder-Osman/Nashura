"""Neutral environment advancement for both sides of an event policy."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..contracts import ActionV1
from ..match_factory import NATIVE_GAMEPLAY_END_TICK

EVENT_SCHEDULER_VERSION = "event-scheduling.v3"


def gameplay_end_tick(environment) -> int:
    mode = getattr(environment, "mode_timeline", None)
    return int(mode.timeline.maximum_tick) if mode is not None else NATIVE_GAMEPLAY_END_TICK


def policy_actions_open(environment, tick: int) -> bool:
    return tick < gameplay_end_tick(environment) and bool(getattr(environment, "policy_actions_open", True))


@dataclass
class EventExecutionLedger:
    """Count command resolution, never treat a queue receipt as execution."""

    pending: dict[str, int] = field(default_factory=dict)
    resolved: set[str] = field(default_factory=set)
    submitted: int = 0
    confirmed: int = 0
    rejected: int = 0
    cancelled_terminal: int = 0
    confirmed_by_owner: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0})
    pending_abilities: dict[str, ActionV1] = field(default_factory=dict)
    ability_events: list = field(default_factory=list)
    confirmed_abilities: int = 0

    def submit(self, action: ActionV1) -> None:
        identity = action.action_id
        if not identity or identity in self.pending or identity in self.resolved:
            raise ValueError("event command needs a unique action_id")
        self.pending[identity] = action.owner
        if action.kind.value == "activate_ability":
            self.pending_abilities[identity] = action
        self.submitted += 1

    def reconcile(self, outcomes) -> set[int]:
        retry = set()
        self.ability_events = []
        for event in outcomes:
            identity = event.data.get("action_id")
            if not identity:
                continue  # Public native plays not associated with our commands.
            if identity not in self.pending:
                raise ValueError(f"unexpected or duplicate action outcome: {identity}")
            owner = self.pending.pop(identity)
            if event.owner != owner:
                raise ValueError("action outcome belongs to a different owner")
            ability = self.pending_abilities.pop(identity, None)
            self.resolved.add(identity)
            if event.event_type == "action_executed":
                if ability is not None:
                    if (event.data.get("kind") != "activate_ability"
                            or event.data.get("ability_id") != ability.ability_id
                            or event.entity_id != ability.source_entity
                            or not event.data.get("fair_ability_activation_exact")):
                        raise ValueError("ability receipt does not attest its exact source and identity")
                    self.ability_events.append(event)
                    self.confirmed_abilities += 1
                self.confirmed += 1
                self.confirmed_by_owner[owner] += 1
            elif event.event_type == "action_rejected":
                self.rejected += 1
                retry.add(owner)
            elif event.event_type == "action_canceled_terminal":
                self.cancelled_terminal += 1
            else:
                raise ValueError(f"unexpected action resolution: {event.event_type}")
        return retry

    def counters(self) -> dict[str, int]:
        return dict(submitted=self.submitted, executed=self.confirmed, confirmed=self.confirmed,
                    rejected=self.rejected, cancelled_native_terminal=self.cancelled_terminal,
                    unconfirmed=len(self.pending))


def complete_event_commands(
    commands: Mapping[int, tuple[ActionV1, ...]],
) -> dict[int, tuple[ActionV1, ...]]:
    """Keep an idle owner ready at the next event boundary.

    BattleEnv interprets an omitted owner as a fixed decision-period WAIT.
    Event policies already schedule their own plans and timeouts, so omission
    must instead mean a neutral one-tick advance. Preserve explicit waits and
    plays, including their independent deadlines.
    """

    return {
        owner: commands.get(owner) or (ActionV1.wait(owner, ticks=1),)
        for owner in (0, 1)
    }
