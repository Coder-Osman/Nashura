"""Observation-only performance probes for real SSPI native-search roots.

Do not add nested native RPC or opponent sampling seconds to the disjoint
top-level CompletedBoundarySearch phase totals; those categories overlap.
"""

from time import perf_counter


class NativeRequestProfiler:
    """Measure existing native commands without adding extra native requests."""

    def __init__(self):
        self.commands = {}
        self.phases = {}
        self.successful_step_ticks_requested = 0

    def record(self, command: str, seconds: float, succeeded: bool) -> None:
        kind = command.partition(" ")[0]
        if kind not in {
            "step", "observe", "observe-atomic", "observe-atomic-delta", "snapshot-create", "restore",
            "release-snapshot", "state-digest", "queue-hand-action-at",
        }:
            kind = "other"
        item = self.commands.setdefault(kind, dict(calls=0, successes=0, seconds=0.0))
        item["calls"] += 1
        item["successes"] += int(succeeded)
        item["seconds"] += seconds
        if kind == "step" and succeeded:
            self.successful_step_ticks_requested += int(command.partition(" ")[2])

    def record_phase(self, command: str, phase: str, seconds: float, *, response_bytes: int = 0) -> None:
        kind = command.partition(" ")[0]
        if kind not in {
            "step", "observe", "observe-atomic", "observe-atomic-delta", "snapshot-create", "restore",
            "release-snapshot", "state-digest", "queue-hand-action-at",
        }:
            kind = "other"
        item = self.phases.setdefault(kind, {}).setdefault(phase, dict(calls=0, seconds=0.0, response_bytes=0))
        item["calls"] += 1
        item["seconds"] += seconds
        item["response_bytes"] += response_bytes

    def summary(self) -> dict:
        return dict(
            commands={key: dict(value) for key, value in sorted(self.commands.items())},
            request_phases={
                command: {phase: dict(value) for phase, value in sorted(phases.items())}
                for command, phases in sorted(self.phases.items())
            },
            successful_step_ticks_requested=self.successful_step_ticks_requested,
            tick_semantics="requested ticks of successful native step commands; not observed tick deltas",
        )


class TimedSearchInference:
    """Measure leaf inference and opponent policy sampling without changing RNG."""

    def __init__(self, inference):
        self.inference = inference
        self.checkpoint_sha = inference.checkpoint_sha
        self.controller_sha = inference.controller_sha
        self.predict_calls = 0
        self.predict_seconds = 0.0
        self.opponent_sample_calls = 0
        self.opponent_sample_seconds = 0.0

    def predict(self, information):
        started = perf_counter()
        try:
            return self.inference.predict(information)
        finally:
            self.predict_calls += 1
            self.predict_seconds += perf_counter() - started

    def sample(self, information, generator):
        started = perf_counter()
        try:
            return self.inference.sample(information, generator)
        finally:
            self.opponent_sample_calls += 1
            self.opponent_sample_seconds += perf_counter() - started

    def summary(self) -> dict:
        return dict(
            predict_calls=self.predict_calls,
            predict_seconds=self.predict_seconds,
            opponent_sample_calls=self.opponent_sample_calls,
            opponent_sample_seconds=self.opponent_sample_seconds,
            note="opponent_sample_seconds is nested inside transition_seconds",
        )


class NativeTickCounter:
    """Accumulate actual successful clock advances across belief rollouts."""

    def __init__(self):
        self.advance_calls = 0
        self.actual_ticks = 0
        self.advance_seconds = 0.0

    def advance(self, rollout, call):
        before = rollout.runtime.clock.tick
        started = perf_counter()
        try:
            result = call()
        finally:
            self.advance_seconds += perf_counter() - started
        after = rollout.runtime.clock.tick
        if after <= before:
            raise ValueError("successful search advance did not progress a native tick")
        self.actual_ticks += after - before
        self.advance_calls += 1
        return result

    def summary(self) -> dict:
        return dict(actual_ticks=self.actual_ticks, advance_calls=self.advance_calls,
                    advance_seconds=self.advance_seconds)
