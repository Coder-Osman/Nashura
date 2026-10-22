"""Policy delay constraints, independent of checkpoint architecture and torch."""

from collections.abc import Sequence

MINIMUM_POLICY_DELAY_MS = 100


def delay_constraints_contract() -> dict[str, object]:
    return {"delay": {"minimum_ms": MINIMUM_POLICY_DELAY_MS}}


def playable_delay_mask(
    delay_offset_ms: Sequence[int], *, minimum_delay_ms: int = 0,
    tick: int = 0, end_tick: int | None = None,
) -> tuple[bool, ...]:
    """Intersect the policy floor, affordability, and optional native deadline."""
    minimum = max(MINIMUM_POLICY_DELAY_MS, int(minimum_delay_ms))
    return tuple(
        int(offset) >= minimum
        and (end_tick is None or tick + max(1, (int(offset) + 49) // 50) < end_tick)
        for offset in delay_offset_ms
    )
