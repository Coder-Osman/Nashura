"""Per-player supervised decision boundaries, in the native 20 Hz clock.

Replay commands provide labels, never input features. A public, executed action
interrupts both players. A WAIT deadline is measured from that player's most
recent decision, not from a global three-second grid.
"""
from dataclasses import dataclass, field


@dataclass
class EventILClock:
    first_tick: int
    max_wait_ticks: int = 60
    deadlines: dict[int, int] = field(init=False)
    pending: dict[int, tuple[int, int]] = field(default_factory=dict)

    def __post_init__(self):
        if self.first_tick < 0 or self.max_wait_ticks <= 0:
            raise ValueError("invalid decision clock")
        self.deadlines = {0: self.first_tick, 1: self.first_tick}

    def wake(self, tick, *, public=False, terminal=False):
        if terminal:
            return set()
        if public:
            return {0, 1}
        return {owner for owner, due in self.deadlines.items() if tick >= due}

    def decide(self, owner, tick, *, source_index=None, execution_tick=None):
        if owner not in (0, 1) or tick < self.first_tick:
            raise ValueError("invalid decision identity")
        self.pending.pop(owner, None)
        if source_index is None:
            if execution_tick is not None:
                raise ValueError("WAIT has no planned execution")
            self.deadlines[owner] = tick + self.max_wait_ticks
        else:
            if execution_tick is None or execution_tick <= tick:
                raise ValueError("an action needs a future native receipt tick")
            self.pending[owner] = (source_index, execution_tick)
            self.deadlines[owner] = execution_tick

    def next_tick(self, current, boundaries, end_tick):
        candidates = [end_tick, *self.deadlines.values(), *boundaries]
        future = [tick for tick in candidates if tick > current]
        if not future:
            raise ValueError("decision clock has no future boundary")
        return min(future)
