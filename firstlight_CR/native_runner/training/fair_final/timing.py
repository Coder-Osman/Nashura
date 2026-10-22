from copy import deepcopy
from dataclasses import asdict, dataclass

from .policy import FullAction


@dataclass(frozen=True)
class Reservation:
    identity: int
    owner: int
    action: FullAction
    created_tick: int
    due_tick: int

    def __post_init__(self):
        if (type(self.identity) is not int or self.identity < 0 or type(self.owner) is not int
                or self.owner not in (0, 1) or not isinstance(self.action, FullAction)
                or type(self.created_tick) is not int or self.created_tick < 0
                or type(self.due_tick) is not int):
            raise ValueError("invalid reservation identity or timestamp")
        duration = 60 if self.action.card_id is None else 2 * self.action.delay_slot
        if self.due_tick != self.created_tick + duration:
            raise ValueError("reservation deadline differs from its action")


class DecisionClock:
    schema = "fair-final-decision-clock.v1"

    def __init__(self):
        self.tick = -1
        self.next_identity = 0
        self.pending = {}
        self.ready = set()
        self.evidence = []

    def begin_tick(self, tick, *, wake_owners, adjudicate_due, freeze):
        wake_owners = tuple(wake_owners)
        if type(tick) is not int or tick < 0 or tick <= self.tick or self.ready:
            raise ValueError("tick must advance after both frozen decisions are committed")
        if any(type(owner) is not int or owner not in (0, 1) for owner in wake_owners):
            raise ValueError("invalid FAIR wake owner")
        due = tuple(sorted((plan for plan in self.pending.values() if plan.due_tick <= tick),
                           key=lambda plan: (plan.due_tick, plan.owner)))
        if any(plan.due_tick != tick for plan in due):
            raise ValueError("native adapter skipped a reservation deadline")
        actions = tuple(plan for plan in due if plan.action.card_id is not None)
        outcomes = adjudicate_due(actions, tick) if actions else {}
        if set(outcomes) != {plan.identity for plan in actions} or any(
                reason not in ("executed", "rejected") for reason in outcomes.values()):
            raise ValueError("native due settlement needs complete execution evidence")
        self.tick = tick
        owners = set(wake_owners)
        for plan in due:
            reason = "wait_expired" if plan.action.card_id is None else outcomes[plan.identity]
            self._resolve(plan, reason)
            owners.add(plan.owner)
        for owner in sorted(owners):
            if owner in self.pending:
                self._resolve(self.pending[owner], "preempted")
        frozen = {owner: freeze(owner, tick) for owner in sorted(owners)}
        self.ready = owners
        return frozen

    def _resolve(self, plan, reason):
        self.evidence.append(dict(reservation=plan.identity, owner=plan.owner, tick=self.tick, reason=reason))
        del self.pending[plan.owner]

    def commit(self, actions, *, legal):
        if not self.ready or set(actions) != self.ready:
            raise ValueError("submit all frozen same-tick decisions atomically")
        if any(not isinstance(action, FullAction) or not legal(owner, action) for owner, action in actions.items()):
            raise ValueError("illegal decision; no reservation committed")
        for owner, action in sorted(actions.items()):
            ticks = 60 if action.card_id is None else action.delay_slot * 2
            self.pending[owner] = Reservation(self.next_identity, owner, action, self.tick, self.tick + ticks)
            self.next_identity += 1
        self.ready.clear()

    def boundary(self, owner):
        return owner in self.ready and owner not in self.pending

    def finish(self, tick, *, outcomes):
        if self.ready or type(tick) is not int or tick <= self.tick:
            raise ValueError("terminal tick must follow committed decisions")
        due = [plan for plan in self.pending.values() if plan.due_tick <= tick]
        if any(plan.due_tick != tick for plan in due):
            raise ValueError("terminal advance skipped a reservation deadline")
        expected = {plan.identity for plan in due if plan.action.card_id is not None}
        if set(outcomes) != expected or any(reason not in ("executed", "rejected") for reason in outcomes.values()):
            raise ValueError("terminal reservation attestation is incomplete")
        self.tick = tick
        for plan in due:
            self._resolve(plan, "wait_expired" if plan.action.card_id is None else outcomes[plan.identity])
        for plan in tuple(self.pending.values()):
            self._resolve(plan, "terminal_expired")

    def capture(self):
        return deepcopy(dict(schema=self.schema, tick=self.tick, next_identity=self.next_identity,
            pending=[asdict(plan) for plan in self.pending.values()], ready=sorted(self.ready), evidence=self.evidence))

    @classmethod
    def restore(cls, payload):
        if set(payload) != {"schema", "tick", "next_identity", "pending", "ready", "evidence"} or payload["schema"] != cls.schema:
            raise ValueError("decision clock snapshot identity differs")
        result = cls()
        result.tick, result.next_identity = payload["tick"], payload["next_identity"]
        if (type(result.tick) is not int or result.tick < -1 or type(result.next_identity) is not int
                or result.next_identity < 0):
            raise ValueError("invalid restored clock counters")
        identities = set()
        for raw in payload["pending"]:
            plan = Reservation(**{**raw, "action": FullAction(**raw["action"])})
            if (plan.owner in result.pending or plan.due_tick <= result.tick or plan.identity >= result.next_identity
                    or plan.created_tick > result.tick or plan.identity in identities):
                raise ValueError("invalid restored reservation")
            identities.add(plan.identity)
            result.pending[plan.owner] = plan
        result.ready = set(payload["ready"])
        if result.ready & result.pending.keys() or not result.ready <= {0, 1}:
            raise ValueError("restored decision boundary has unresolved reservations")
        result.evidence = deepcopy(payload["evidence"])
        return result
