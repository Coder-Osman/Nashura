from dataclasses import dataclass, field
import math
import random
import time

from .config import JointConfig, stream_seed


@dataclass
class JointNode:
    prior: dict
    value: float
    visits: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)

    def __post_init__(self):
        if (not 1 <= len(self.prior) <= 32 or "wait" not in self.prior
                or any(not math.isfinite(value) or value < 0 for value in self.prior.values())
                or not math.isclose(sum(self.prior.values()), 1., abs_tol=1e-5)
                or not math.isfinite(self.value) or not -1 <= self.value <= 1):
            raise ValueError("invalid joint node prior or value")
        self.visits = dict.fromkeys(self.prior, 0)
        self.totals = dict.fromkeys(self.prior, 0.)

    def select(self, config):
        count = sum(self.visits.values())
        width = min(len(self.prior), 8 + count // 4)
        ranked = sorted((action for action in self.prior if action != "wait"),
                        key=lambda action: (-self.prior[action], action))
        opened = ["wait", *ranked[:width - 1]]

        def score(action):
            visits = self.visits[action]
            mean = self.totals[action] / visits if visits else self.value
            return mean + config.cpuct * self.prior[action] * math.sqrt(count + 1) / (1 + visits)

        return min(opened, key=lambda action: (-score(action), action))


@dataclass(frozen=True)
class JointSearchResult:
    visits: dict
    prior: dict
    noisy_prior: dict
    seconds: float
    timings: dict


class JointSearch:
    def __init__(self, belief, inference, *, config=None):
        self.belief, self.inference = belief, inference
        self.config = config or JointConfig()

    def run(self, information, *, seed, training, deadline=None):
        started = time.perf_counter()
        config = self.config
        timings = {name: 0. for name in ("belief", "restore", "inference", "apply", "advance")}
        worlds = []

        def timed(name, operation, *arguments, **keywords):
            before = time.perf_counter()
            try:
                return operation(*arguments, **keywords)
            finally:
                timings[name] += time.perf_counter() - before

        def check_deadline():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("incomplete 128-simulation joint root")

        def predict(current):
            prior, value = timed("inference", self.inference.predict, current,
                                 seed=stream_seed("candidates", seed, current.sha256))
            if any(not current.legal(action) for action in prior):
                raise ValueError("illegal joint candidate")
            return JointNode(prior, value)

        try:
            worlds = list(timed("belief", self.belief.build, information,
                                seed=stream_seed("belief", seed), limit=1))
            if len(worlds) != 1:
                raise ValueError("joint search requires exactly one compatible FAIR world")
            world = worlds[0]
            root = predict(information)
            prior = dict(root.prior)
            if training and config.root_noise:
                rng = random.Random(stream_seed("root-noise", seed))
                draws = {action: rng.gammavariate(config.noise_concentration / len(prior), 1.) for action in prior}
                mass = sum(draws.values())
                root.prior = {action: (1 - config.root_noise) * probability
                              + config.root_noise * draws[action] / mass for action, probability in prior.items()}
            signature = information.sha256
            nodes = {signature: root}
            for ordinal in range(128):
                check_deadline()
                timed("restore", world.restore_root)
                if world.root_information().sha256 != signature:
                    raise ValueError("joint snapshot restored a different FAIR root")
                current, node, path = information, root, []
                decisions = 0
                while True:
                    check_deadline()
                    terminal = world.terminal_result(information.owner)
                    if terminal is not None:
                        if type(terminal) is not int or terminal not in (-1, 0, 1):
                            raise ValueError("invalid native terminal value")
                        leaf = float(terminal)
                        break
                    elapsed = (current.tick - information.tick) * .05
                    if elapsed < 0:
                        raise ValueError("joint simulation moved backwards")
                    if decisions >= config.max_decisions or elapsed >= config.max_game_seconds:
                        estimate = timed("inference", self.inference.value, current)
                        leaf = estimate if current.owner == information.owner else -estimate
                        break
                    if current.owner == information.owner:
                        action = node.select(config)
                        path.append((node, action))
                    else:
                        action = timed("inference", self.inference.sample, current,
                            seed=stream_seed("opponent-response", seed, ordinal, decisions, current.sha256))
                    if not current.legal(action):
                        raise ValueError("illegal simulated joint action")
                    timed("apply", world.apply, action)
                    timed("advance", world.advance, max(0., config.max_game_seconds - elapsed))
                    decisions += 1
                    terminal = world.terminal_result(information.owner)
                    if terminal is not None:
                        if type(terminal) is not int or terminal not in (-1, 0, 1):
                            raise ValueError("invalid native terminal value")
                        leaf = float(terminal)
                        break
                    current = world.information()
                    if current.owner == information.owner:
                        key = current.sha256
                        if key not in nodes:
                            nodes[key] = predict(current)
                            leaf = nodes[key].value
                            break
                        node = nodes[key]
                if not math.isfinite(leaf) or not -1 <= leaf <= 1:
                    raise ValueError("invalid joint leaf value")
                for parent, action in path:
                    parent.visits[action] += 1
                    parent.totals[action] += leaf
            if sum(root.visits.values()) != 128:
                raise ValueError("joint root visit budget differs")
            return JointSearchResult(dict(root.visits), prior, dict(root.prior),
                                     time.perf_counter() - started, timings)
        finally:
            failures = []
            for world in worlds:
                try:
                    world.close()
                except Exception as error:
                    failures.append(error)
            if failures:
                raise RuntimeError("joint belief cleanup failed") from failures[0]
