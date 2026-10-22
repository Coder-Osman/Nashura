"""Live click scheduling with command-latency compensation and card-play wakes."""
from __future__ import annotations

from .snapshot import LiveStateError

SCHEDULER_VERSION = "live-card-play-scheduling.v3"
DEFAULT_INPUT_LEAD_MS = 1000


class LiveScheduler:
    def __init__(self, *, max_wait_ticks=300, input_lead_ms=DEFAULT_INPUT_LEAD_MS):
        if isinstance(input_lead_ms, bool) or not isinstance(input_lead_ms, int) or input_lead_ms < 0 or input_lead_ms % 50:
            raise ValueError("input lead must be a nonnegative multiple of 50 ms")
        self.max_wait_ticks = max_wait_ticks
        self.input_lead_ms = input_lead_ms
        self.pending = None
        self.last_decision = None
        self.trigger = "initial"

    def observe(self, row, fresh):
        cancelled = None
        if fresh:
            self.trigger = "card_play"
            if self.pending is not None:
                cancelled, self.pending = self.pending, None
        if self.last_decision is not None and row["tick"] < self.last_decision:
            raise LiveStateError("scheduler tick regressed")
        if (self.trigger is None and self.pending is None and self.last_decision is not None
                and row["tick"]-self.last_decision >= self.max_wait_ticks):
            self.trigger = "wait_timeout"
        return cancelled

    def decide(self, decision):
        self.last_decision = decision["tick"]
        self.trigger = None
        if decision["act"]:
            policy_tick = decision["tick"] + max(1, (decision["delay_ms"]+49)//50)
            # Compensation changes the click deadline, never the sampled delay.
            decision["policy_scheduled_tick"] = policy_tick
            decision["input_lead_ms"] = self.input_lead_ms
            decision["scheduled_tick"] = max(decision["tick"], policy_tick - self.input_lead_ms//50)
            decision["scheduled_delay_ms"] = (decision["scheduled_tick"] - decision["tick"])*50
            self.pending = decision
        else:
            self.pending = None

    def take_due(self, tick):
        if self.pending is None or tick < self.pending["scheduled_tick"]:
            return None
        plan, self.pending = self.pending, None
        return plan

    def recover(self):
        self.trigger = self.trigger or "recovery"
