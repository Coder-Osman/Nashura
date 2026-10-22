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


@dataclass(frozen=True)
class FairHistory:
    opening: OpeningEvidence
    decisions: tuple
    deployments: tuple
    root_tick: int
    root_fair_sha: str

    schema = "fair-final-visible-history.v1"

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

    @property
    def sha256(self):
        return semantic_digest(self.to_payload())

    def to_payload(self):
        return dict(schema=self.schema, **asdict(self))

    @classmethod
    def from_payload(cls, payload):
        if (set(payload) != {"schema", "opening", "decisions", "deployments", "root_tick", "root_fair_sha"}
                or payload["schema"] != cls.schema):
            raise ValueError("FAIR history rejects private audit or unknown fields")
        try:
            opening = OpeningEvidence(**{**payload["opening"], "own_draw": tuple(payload["opening"]["own_draw"])})
            decisions = tuple(OwnDecision(**{**entry, "action": FullAction(**entry["action"])})
                for entry in payload["decisions"])
            deployments = tuple(PublicDeployment(**entry) for entry in payload["deployments"])
            return cls(opening, decisions, deployments, payload["root_tick"], payload["root_fair_sha"])
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
            and any(entry.tick == previous.root_tick and entry.fair_sha == previous.root_fair_sha
                    for entry in self.decisions[len(previous.decisions):])
            and all(entry.tick > previous.root_tick for entry in self.deployments[len(previous.deployments):]))


class FairHistoryRecorder:
    def __init__(self, observation, information, *, opening_tick):
        self.opening = OpeningEvidence.from_view(observation, information, opening_tick=opening_tick)
        self.decisions = []
        self.deployments = []
        self.seen = set()
        self.observed_tick = opening_tick

    def observe_tick(self, observation):
        validate_fair_view(observation, self.opening.owner)
        if observation.tick not in (self.observed_tick, self.observed_tick + 1):
            raise ValueError("FAIR history requires every tick; event windows may discard deployments")
        additions = set()
        for event in observation.events:
            if event.event_type != "action_executed" or not all(
                    event_visible_to_owner(event, owner) for owner in (0, 1)):
                continue
            entry = PublicDeployment.from_event(event)
            if entry.tick > observation.tick:
                raise ValueError("future public execution entered FAIR history")
            if entry not in self.seen:
                additions.add(entry)
        ordered = sorted(additions)
        if ordered and self.deployments and ordered[0] <= self.deployments[-1]:
            raise ValueError("public deployment arrived after its history boundary")
        if len({(entry.tick, entry.owner) for entry in [*self.deployments[-2:], *ordered]}) != len(
                [*self.deployments[-2:], *ordered]):
            raise ValueError("multiple public executions by one owner at a tick")
        self.deployments.extend(ordered)
        self.seen.update(ordered)
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
            information.tick, information.sha256)
