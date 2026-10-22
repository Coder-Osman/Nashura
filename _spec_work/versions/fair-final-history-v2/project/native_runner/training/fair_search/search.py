from dataclasses import dataclass, field
import math
import random
import time
from typing import Protocol, Sequence

from .config import SearchConfig
from .contracts import InformationState


class BeliefWorld(Protocol):
    def restore_root(self) -> None: ...
    def root_information(self) -> InformationState: ...
    def information(self) -> InformationState: ...
    def apply(self, action: str) -> None: ...
    def advance(self, remaining_seconds: float) -> None: ...
    def terminal_result(self, owner: int) -> int | None: ...
    def close(self) -> None: ...


class BeliefProvider(Protocol):
    def build(self, information: InformationState, *, seed: int, limit: int) -> Sequence[BeliefWorld]: ...


class SearchInferenceClient(Protocol):
    def predict(self, information: InformationState, *, seed: int) -> tuple[dict[str, float], float]: ...
    def sample(self, information: InformationState, *, seed: int) -> str: ...


@dataclass
class Node:
    prior: dict[str, float]
    value: float
    visits: dict[str, int] = field(default_factory=dict)
    totals: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.prior or len(self.prior) > 32 or "wait" not in self.prior:
            raise ValueError("candidate prior contract differs")
        if not math.isfinite(self.value) or not -1 <= self.value <= 1:
            raise ValueError("invalid bounded leaf value")
        if any(not math.isfinite(value) or value < 0 for value in self.prior.values()):
            raise ValueError("invalid candidate prior")
        total = sum(self.prior.values())
        if total <= 0:
            raise ValueError("empty prior mass")
        self.prior = {action: mass / total for action, mass in self.prior.items()}
        self.visits = {action: 0 for action in self.prior}
        self.totals = {action: 0. for action in self.prior}

    def select(self, config):
        count = sum(self.visits.values())
        width = min(len(self.prior), max(config.initial_candidates, int(math.ceil(4 * math.sqrt(count + 1)))))
        ranked = sorted(self.prior, key=lambda action: (-self.prior[action], action))
        candidates = ["wait", *[action for action in ranked if action != "wait"][:width - 1]]

        def score(action):
            visits = self.visits[action]
            mean = self.totals[action] / visits if visits else self.value
            exploration = config.cpuct * self.prior[action] * math.sqrt(count + 1) / (1 + visits)
            return mean + exploration

        return min(candidates, key=lambda action: (-score(action), action))


@dataclass(frozen=True)
class SearchResult:
    visits: dict[str, int]
    selected: str
    seconds: float
    particles: int
    timings: dict[str, float] = field(default_factory=dict)
    audit: dict = field(default_factory=dict)


class InformationSetSearch:
    def __init__(self, belief, inference, *, config=None):
        self.belief, self.inference = belief, inference
        self.config = config or SearchConfig()

    def run(self, information, *, seed, training=True, deadline=None, diagnostic_simulations=None, audit_root=False):
        config = self.config
        if diagnostic_simulations not in (None, 32, 128):
            raise ValueError("diagnostic search budget must be 32 or 128")
        budget = config.simulations if diagnostic_simulations is None else diagnostic_simulations
        rng = random.Random(seed)
        started = time.perf_counter()
        worlds = []
        timings = {name: 0. for name in ("belief_seconds", "restore_seconds", "inference_seconds",
                                        "advance_seconds", "apply_seconds")}

        def timed(name, operation, *arguments, **keywords):
            before = time.perf_counter()
            try:
                return operation(*arguments, **keywords)
            finally:
                timings[name] += time.perf_counter() - before

        try:
            worlds = list(timed("belief_seconds", self.belief.build, information,
                                seed=rng.randrange(2**63), limit=config.particles))
            if not 1 <= len(worlds) <= config.particles:
                raise ValueError("no valid bounded FAIR belief")
            root_signature = information.sha256
            for world in worlds:
                timed("restore_seconds", world.restore_root)
                if world.root_information().sha256 != root_signature:
                    raise ValueError("belief world differs from root information set")
            prior, value = timed("inference_seconds", self.inference.predict, information,
                                 seed=rng.randrange(2**63))
            if any(not information.legal(action) for action in prior):
                raise ValueError("candidate generator returned illegal actions")
            root = Node(prior, value)
            original_prior = dict(root.prior) if audit_root else None
            if training and config.root_noise:
                draws = {action: rng.gammavariate(config.noise_concentration / len(prior), 1.) for action in prior}
                total = sum(draws.values())
                root.prior = {action: (1 - config.root_noise) * root.prior[action]
                              + config.root_noise * draws[action] / total for action in prior}
            nodes = {root_signature: root}
            for ordinal in range(budget):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("deadline before complete 32-simulation root")
                world = rng.choice(worlds)
                timed("restore_seconds", world.restore_root)
                if world.root_information().sha256 != root_signature:
                    raise ValueError("restored FAIR root changed")
                node, current = root, information
                path = []
                decisions = 0
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("deadline inside root simulation")
                    terminal = world.terminal_result(information.owner)
                    if terminal is not None:
                        if type(terminal) is not int or terminal not in (-1, 0, 1):
                            raise ValueError("invalid native terminal return")
                        leaf = float(terminal)
                        break
                    elapsed = (current.tick - information.tick) * .05
                    if elapsed < 0:
                        raise ValueError("simulation went backwards in game time")
                    if decisions >= config.max_decisions or elapsed >= config.max_game_seconds:
                        _, estimate = timed("inference_seconds", self.inference.predict, current,
                                            seed=rng.randrange(2**63))
                        leaf = estimate if current.owner == information.owner else -estimate
                        break
                    if current.owner == information.owner:
                        action = node.select(config)
                        path.append((node, action))
                    else:
                        action = timed("inference_seconds", self.inference.sample, current,
                                       seed=rng.randrange(2**63))
                    if not current.legal(action):
                        raise ValueError("simulation selected an illegal action")
                    timed("apply_seconds", world.apply, action)
                    timed("advance_seconds", world.advance, max(0., config.max_game_seconds - elapsed))
                    decisions += 1
                    terminal = world.terminal_result(information.owner)
                    if terminal is not None:
                        leaf = float(terminal)
                        break
                    current = world.information()
                    if current.owner == information.owner:
                        signature = current.sha256
                        if signature not in nodes:
                            prior, estimate = timed("inference_seconds", self.inference.predict, current,
                                                     seed=rng.randrange(2**63))
                            if any(not current.legal(action) for action in prior):
                                raise ValueError("descendant candidate is illegal")
                            nodes[signature] = Node(prior, estimate)
                            leaf = estimate
                            break
                        node = nodes[signature]
                if not math.isfinite(leaf) or not -1 <= leaf <= 1:
                    raise ValueError("leaf value is not a bounded win expectation")
                for parent, action in path:
                    parent.visits[action] += 1
                    parent.totals[action] += leaf
            if sum(root.visits.values()) != budget:
                raise ValueError("root did not complete requested visits")
            if training:
                temperature = 1. if information.tick * .05 < config.exploration_seconds else config.late_temperature
                actions = sorted(action for action, count in root.visits.items() if count > 0)
                if not actions:
                    raise ValueError("complete search produced no visited root action")
                selected = rng.choices(actions, [root.visits[action] ** (1 / temperature) for action in actions])[0]
            else:
                selected = min(root.visits, key=lambda action: (-root.visits[action], action))
            seconds = time.perf_counter() - started
            timings["other_seconds"] = max(0., seconds - sum(timings.values()))
            audit = dict(simulations=budget, seed=seed, root_information_sha256=root_signature,
                         prior=original_prior, effective_prior=dict(root.prior), root_value=root.value,
                         totals=dict(root.totals), q={key: root.totals[key] / count if count else None
                                                    for key, count in root.visits.items()}) if audit_root else {}
            return SearchResult(dict(root.visits), selected, seconds, len(worlds), timings, audit)
        finally:
            failures = []
            for world in worlds:
                try:
                    world.close()
                except Exception as error:
                    failures.append(error)
            if failures:
                raise RuntimeError("belief cleanup failed") from failures[0]
