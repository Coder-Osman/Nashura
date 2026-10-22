from dataclasses import asdict, dataclass

from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import require_sha, semantic_digest
from .opening_belief import OpeningEvidence
from .policy import FullAction
from .visibility import event_visible_to_owner, validate_fair_view


@dataclass(frozen=True, order=True)
class PublicDeployment:
    tick: int
    owner: int
    card_id: int
    x: int
    y: int

    def __post_init__(self):
        if (type(self.tick) is not int or self.tick < 0 or type(self.owner) is not int
                or self.owner not in (0, 1) or type(self.card_id) is not int or self.card_id not in HOG_DECK
                or type(self.x) is not int or type(self.y) is not int
                or not 0 <= self.x < 18000 or not 0 <= self.y < 32000):
            raise ValueError("invalid public deployment")

    @classmethod
    def from_event(cls, event):
        if (event.event_type != "action_executed" or event.position is None
                or not all(event_visible_to_owner(event, owner) for owner in (0, 1))):
            raise ValueError("deployment must be publicly executed for both players")
        position = event.position
        coordinates = position if isinstance(position, (tuple, list)) else (position.x, position.y)
        if len(coordinates) != 2 or any(float(value) != round(float(value)) for value in coordinates):
            raise ValueError("deployment coordinates must be exact native integers")
        return cls(event.tick, event.owner, event.card_id, *(int(value) for value in coordinates))


@dataclass(frozen=True)
class OwnDecision:
    tick: int
    fair_sha: str
    action: FullAction

    def __post_init__(self):
        require_sha(self.fair_sha)
        if type(self.tick) is not int or self.tick < 0 or not isinstance(self.action, FullAction):
            raise ValueError("invalid own decision evidence")


@dataclass(frozen=True, order=True)
class OwnRejection:
    tick: int
    decision_tick: int

    def __post_init__(self):
        if (type(self.tick) is not int or type(self.decision_tick) is not int
                or self.decision_tick < 0 or not self.decision_tick < self.tick <= self.decision_tick + 60
                or (self.tick - self.decision_tick) % 2):
            raise ValueError("invalid own rejection timing")

    @classmethod
    def from_event(cls, event, *, owner, decision):
        if (not isinstance(decision, OwnDecision) or event.event_type != "action_rejected"
                or type(event.owner) is not int or event.owner != owner
                or type(event.card_id) is not int or type(event.data.get("private_to")) is not int
                or event.data["private_to"] != owner or not event_visible_to_owner(event, owner)
                or decision.action.card_id is None or event.card_id != decision.action.card_id
                or event.tick != decision.tick + 2 * decision.action.delay_slot):
            raise ValueError("rejection must attest this owner's due ACT decision")
        return cls(event.tick, decision.tick)


@dataclass(frozen=True)
class FairHistory:
    opening: OpeningEvidence
    decisions: tuple
    deployments: tuple
    root_tick: int
    root_fair_sha: str
    rejections: tuple = ()

    schema = "fair-final-visible-history.v2"

    def __post_init__(self):
        opening = self.opening
        if not isinstance(opening, OpeningEvidence):
            raise ValueError("history requires typed opening evidence")
        require_sha(opening.root_fair_sha)
        require_sha(self.root_fair_sha)
        if (type(opening.owner) is not int or opening.owner not in (0, 1)
                or type(opening.tick) is not int or opening.tick < 0
                or type(opening.own_draw) is not tuple or len(opening.own_draw) != 8
                or set(opening.own_draw) != set(HOG_DECK)
                or type(self.root_tick) is not int or self.root_tick < opening.tick
                or type(self.decisions) is not tuple or type(self.deployments) is not tuple):
            raise ValueError("invalid FAIR history identity")
        if any(not isinstance(entry, OwnDecision) for entry in self.decisions):
            raise ValueError("only own typed decisions belong in FAIR history")
        ticks = [entry.tick for entry in self.decisions]
        if (ticks != sorted(set(ticks)) or any(not opening.tick <= tick < self.root_tick for tick in ticks)
                or self.root_tick > opening.tick and (not ticks or ticks[0] != opening.tick)
                or self.decisions and self.decisions[0].fair_sha != opening.root_fair_sha):
            raise ValueError("own history is incomplete or out of order")
        if any(not isinstance(entry, PublicDeployment) for entry in self.deployments):
            raise ValueError("only public typed deployments belong in FAIR history")
        if (tuple(sorted(set(self.deployments))) != self.deployments
                or len({(entry.tick, entry.owner) for entry in self.deployments}) != len(self.deployments)
                or any(not opening.tick < entry.tick <= self.root_tick for entry in self.deployments)):
            raise ValueError("public history is duplicated or out of order")
        if self.root_tick == opening.tick and self.root_fair_sha != opening.root_fair_sha:
            raise ValueError("opening FAIR identity changed")
        if (type(self.rejections) is not tuple
                or any(not isinstance(entry, OwnRejection) for entry in self.rejections)
                or tuple(sorted(set(self.rejections))) != self.rejections
                or len({entry.tick for entry in self.rejections}) != len(self.rejections)):
            raise ValueError("own rejection history is duplicated or out of order")
        decisions = {entry.tick: entry for entry in self.decisions}
        for entry in self.rejections:
            decision = decisions.get(entry.decision_tick)
            if (decision is None or decision.action.card_id is None or entry.tick > self.root_tick
                    or entry.tick != decision.tick + 2 * decision.action.delay_slot
                    or any(decision.tick < tick < entry.tick for tick in ticks)
                    or any(decision.tick < play.tick < entry.tick
                        or play.tick == entry.tick and play.owner == opening.owner for play in self.deployments)):
                raise ValueError("own rejection differs from an unpreempted due ACT")

    @property
    def sha256(self):
        return semantic_digest(self.to_payload())

    def to_payload(self):
        return dict(schema=self.schema, **asdict(self))

    @classmethod
    def from_payload(cls, payload):
        if (set(payload) != {"schema", "opening", "decisions", "deployments", "root_tick", "root_fair_sha", "rejections"}
                or payload["schema"] != cls.schema):
            raise ValueError("FAIR history rejects private audit or unknown fields")
        try:
            opening = OpeningEvidence(**{**payload["opening"], "own_draw": tuple(payload["opening"]["own_draw"])})
            decisions = tuple(OwnDecision(**{**entry, "action": FullAction(**entry["action"])})
                for entry in payload["decisions"])
            deployments = tuple(PublicDeployment(**entry) for entry in payload["deployments"])
            rejections = tuple(OwnRejection(**entry) for entry in payload["rejections"])
            return cls(opening, decisions, deployments, payload["root_tick"], payload["root_fair_sha"], rejections)
        except (TypeError, KeyError) as error:
            raise ValueError("FAIR history contains an invalid record") from error

    def extends(self, previous):
        if not isinstance(previous, FairHistory):
            return False
        if self.root_tick == previous.root_tick:
            return self == previous
        return (self.opening == previous.opening and self.root_tick >= previous.root_tick
            and self.decisions[:len(previous.decisions)] == previous.decisions
            and self.deployments[:len(previous.deployments)] == previous.deployments
            and self.rejections[:len(previous.rejections)] == previous.rejections
            and any(entry.tick == previous.root_tick and entry.fair_sha == previous.root_fair_sha
                    for entry in self.decisions[len(previous.decisions):])
            and all(entry.tick > previous.root_tick for entry in self.deployments[len(previous.deployments):])
            and all(entry.tick > previous.root_tick for entry in self.rejections[len(previous.rejections):]))


class FairHistoryRecorder:
    def __init__(self, observation, information, *, opening_tick):
        self.opening = OpeningEvidence.from_view(observation, information, opening_tick=opening_tick)
        self.decisions = []
        self.deployments = []
        self.seen = set()
        self.rejections = []
        self.seen_rejections = set()
        self.observed_tick = opening_tick

    def observe_tick(self, observation):
        validate_fair_view(observation, self.opening.owner)
        if observation.tick not in (self.observed_tick, self.observed_tick + 1):
            raise ValueError("FAIR history requires every tick; event windows may discard deployments")
        additions = set()
        rejections = set()
        for event in observation.events:
            if (event.event_type == "action_rejected" and event.owner == self.opening.owner
                    and event_visible_to_owner(event, self.opening.owner)):
                decisions = [entry for entry in self.decisions if entry.tick < event.tick]
                if not decisions:
                    raise ValueError("own rejection lacks its recorded ACT decision")
                rejected = OwnRejection.from_event(event, owner=self.opening.owner, decision=decisions[-1])
                if rejected.tick > observation.tick:
                    raise ValueError("future own rejection entered FAIR history")
                if rejected not in self.seen_rejections:
                    if rejected.tick <= self.observed_tick:
                        raise ValueError("own rejection arrived after its history boundary")
                    rejections.add(rejected)
            if event.event_type != "action_executed" or not all(
                    event_visible_to_owner(event, owner) for owner in (0, 1)):
                continue
            entry = PublicDeployment.from_event(event)
            if entry.tick > observation.tick:
                raise ValueError("future public execution entered FAIR history")
            if entry not in self.seen:
                if entry.tick <= self.observed_tick:
                    raise ValueError("public deployment arrived after its history boundary")
                additions.add(entry)
        ordered = sorted(additions)
        if ordered and self.deployments and ordered[0] <= self.deployments[-1]:
            raise ValueError("public deployment arrived after its history boundary")
        if len({(entry.tick, entry.owner) for entry in [*self.deployments[-2:], *ordered]}) != len(
                [*self.deployments[-2:], *ordered]):
            raise ValueError("multiple public executions by one owner at a tick")
        rejected = sorted(rejections)
        if rejected and self.rejections and rejected[0] <= self.rejections[-1]:
            raise ValueError("own rejection arrived after its history boundary")
        for entry in rejected:
            if any(entry.decision_tick < play.tick < entry.tick
                    or play.tick == entry.tick and play.owner == self.opening.owner
                    for play in (*self.deployments, *ordered)):
                raise ValueError("own rejection conflicts with public execution or preemption")
        self.deployments.extend(ordered)
        self.seen.update(ordered)
        self.rejections.extend(rejected)
        self.seen_rejections.update(rejected)
        self.observed_tick = observation.tick

    def record_decision(self, information, action):
        if (information.owner != self.opening.owner or information.tick != self.observed_tick
                or self.decisions and information.tick <= self.decisions[-1].tick
                or not information.legality.legal(action)):
            raise ValueError("own decision differs from frozen FAIR boundary")
        self.decisions.append(OwnDecision(information.tick, information.sha256, action))

    def capture(self, information):
        if information.owner != self.opening.owner or information.tick != self.observed_tick:
            raise ValueError("history root is not the observed own boundary")
        return FairHistory(self.opening, tuple(self.decisions), tuple(self.deployments),
            information.tick, information.sha256, tuple(self.rejections))
