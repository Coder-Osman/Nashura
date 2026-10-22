"""Configuration for the event-driven policy (independent of fixed-step V4)."""

from __future__ import annotations

from dataclasses import dataclass

EVENT_POLICY_VERSION = "event-card-policy.v1"
EVENT_OBSERVATION_VERSION = "event-semantic-observation.v1"
EVENT_DELAY_OFFSETS_MS = tuple(range(0, 6001, 100))
EVENT_DELAY_OFFSETS_3S_MS = tuple(range(0, 3001, 100))


@dataclass(frozen=True, slots=True)
class EventPolicyConfigV1:
    """Fresh event-policy architecture; no legacy checkpoint migration."""

    board_width: int = 18
    board_height: int = 32
    max_micro_actions: int = 1
    delay_offset_ms: tuple[int, ...] = EVENT_DELAY_OFFSETS_MS
    max_wait_ticks: int = 300
    elixir_wake_raw: int = 95_000

    def __post_init__(self) -> None:
        if (self.board_width, self.board_height) != (18, 32):
            raise ValueError("event policy arena grid is frozen at 18x32")
        if self.max_micro_actions != 1:
            raise ValueError("event policy predicts exactly one next card")
        if self.delay_offset_ms not in (EVENT_DELAY_OFFSETS_MS, EVENT_DELAY_OFFSETS_3S_MS):
            raise ValueError("event policy delay vocabulary supports 0..3000ms or 0..6000ms in 100ms steps")
        if self.max_wait_ticks <= 0:
            raise ValueError("event policy maximum wait must be positive")
        if not 0 < self.elixir_wake_raw <= 100_000:
            raise ValueError("event policy elixir wake threshold must be in 1..100000 raw units")

    @property
    def delay_bin_count(self) -> int:
        return len(self.delay_offset_ms)

    @classmethod
    def three_second(cls):
        """New IL contract; legacy checkpoints retain their saved timing."""
        return cls(delay_offset_ms=EVENT_DELAY_OFFSETS_3S_MS, max_wait_ticks=60)

    def delay_to_bin(self, delay_ms: int) -> int:
        """Round to nearest 100ms bin and clamp to the event vocabulary."""

        if delay_ms < 0:
            raise ValueError("event delay must be non-negative")
        nearest = min(self.delay_offset_ms, key=lambda value: (abs(value - int(delay_ms)), value))
        return self.delay_offset_ms.index(nearest)

    def bin_to_delay_ms(self, delay_bin: int) -> int:
        if not 0 <= int(delay_bin) < self.delay_bin_count:
            raise ValueError("event delay bin is outside the vocabulary")
        return self.delay_offset_ms[int(delay_bin)]

    def discount(self, delta_ticks: int, *, tau_ticks: float = 1200.0) -> float:
        """Semi-Markov discount for an event interval."""

        if delta_ticks < 0 or tau_ticks <= 0:
            raise ValueError("event discount inputs are invalid")
        import math

        return math.exp(-float(delta_ticks) / float(tau_ticks))
