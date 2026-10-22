from dataclasses import asdict, dataclass
import math
import random
import time

import torch

from ..fair_search.contracts import require_sha, semantic_digest
from .candidates import act_only_noise, structured_candidates
from .contracts import SEARCH_SCHEMA, validate_search


@dataclass(frozen=True)
class SearchLimits:
    cpuct: float = 1.5
    maximum_candidates: int = 32
    initial_candidates: int = 8
    soft_decisions: int = 6
    soft_ticks: int = 200
    extension_decisions: int = 6
    extension_ticks: int = 60
    maximum_invalid_attempts: int = 32
    root_noise: float = 0.

    def __post_init__(self):
        for name in ("maximum_candidates", "initial_candidates", "soft_decisions", "soft_ticks",
                "extension_decisions", "extension_ticks", "maximum_invalid_attempts"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("positive bounded search limits required")
        if (not 1 <= self.initial_candidates <= self.maximum_candidates <= 32
                or not math.isfinite(self.cpuct) or self.cpuct <= 0
                or not math.isfinite(self.root_noise) or not 0 <= self.root_noise <= 1):
            raise ValueError("invalid search exploration or candidate limits")

    @property
    def sha256(self):
        return semantic_digest(dict(schema="fair-final-completed-search.v1", limits=asdict(self),
            full_budget=128, fast_budget=16, decision_unit="simultaneously-committed-boundary-group",
            opening="WAIT-card-elite-then-prior-sqrt", leaf="root-owner-completed-boundary",
            execution="visits-temperature-one", first_play_value="node-wdl"))


@dataclass(frozen=True)
class Transition:
    completed: bool
    decisions: int
    ticks: int
    information: object = None
    terminal_value: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SearchOutcome:
    result: dict
    selected: object
    policy_target: list | None
    audit: dict


def split_seed(seed, *parts):
    if type(seed) is not int or seed < 0:
        raise ValueError("independent nonnegative search seed required")
    return int(semantic_digest(["fair-final-search-rng.v1", seed, *parts])[:16], 16)


def scalar_value(logits):
    if logits.shape not in ((3,), (1, 3)) or not bool(torch.isfinite(logits).all()):
        raise ValueError("finite WIN/DRAW/LOSS logits required")
    probabilities = logits.detach().reshape(3).softmax(-1)
    return float(probabilities[0] - probabilities[2])


class SearchNode:
    def __init__(self, policy, logits, *, limits, seed, noise=False):
        self.value = scalar_value(logits)
        self.candidates = structured_candidates(policy, seed=seed, maximum=limits.maximum_candidates)
        self.priors = act_only_noise(self.candidates, seed=split_seed(seed, "noise"),
            fraction=limits.root_noise if noise else 0.)
        self.visits = {entry.action.key: 0 for entry in self.candidates}
        self.totals = dict.fromkeys(self.visits, 0.)
        self.mode_mass = policy.conditionals().detach().exp().cpu().tolist()
        mandatory = [entry for entry in self.candidates if entry.proposal in ("WAIT", "CARD_ELITE")]
        remaining = sorted((entry for entry in self.candidates if entry not in mandatory),
            key=lambda entry: (-self.priors[entry.action.key], entry.action.key))
        self.order = (*mandatory, *remaining)
        self.minimum_width = max(len(mandatory), limits.initial_candidates)

    def select(self, limits):
        count = sum(self.visits.values())
        width = min(len(self.order), max(self.minimum_width, math.ceil(4 * math.sqrt(count + 1))))

        def score(entry):
            visits = self.visits[entry.action.key]
            mean = self.totals[entry.action.key] / visits if visits else self.value
            return mean + limits.cpuct * self.priors[entry.action.key] * math.sqrt(count) / (1 + visits)

        return min(self.order[:width], key=lambda entry: (-score(entry), entry.action.key)).action


class CompletedBoundarySearch:
    def __init__(self, belief, inference, opponent, *, limits=None):
        self.belief, self.inference, self.opponent = belief, inference, opponent
        self.limits = limits or SearchLimits()

    def run(self, information, *, seed, behavior_seed, codec, identity, mode="FULL", deadline=None):
        if mode not in ("FULL", "FAST"):
            raise ValueError("search requires FULL or FAST")
        limits = self.limits
        opponent_key = "controller_red_sha" if information.owner == 0 else "controller_blue_sha"
        checkpoint_key = "checkpoint_blue_sha" if information.owner == 0 else "checkpoint_red_sha"
        if (identity["search_protocol_sha"] != limits.sha256
                or identity["root_belief_prior_sha"] != self.belief.sampler_sha
                or identity[opponent_key] != self.opponent.controller_sha
                or identity[checkpoint_key] != self.inference.checkpoint_sha):
            raise ValueError("frozen search, belief or model identity differs")
        for key in (opponent_key, checkpoint_key, "root_belief_prior_sha", "search_protocol_sha"):
            require_sha(identity[key])
        root_sha = information.sha256
        world = None
        started = time.perf_counter()
        timings = dict(belief=0., restore=0., inference=0., transition=0.)
        completed = invalid = discarded = attempts = 0
        lifecycle = {}
        budget = 128 if mode == "FULL" else 16

        def check_deadline():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("search deadline before complete effective budget")

        def timed(name, operation, *args, **kwargs):
            before = time.perf_counter()
            try:
                return operation(*args, **kwargs)
            finally:
                timings[name] += time.perf_counter() - before

        try:
            check_deadline()
            world = timed("belief", self.belief.build, information, seed=split_seed(seed, "world"))
            require_sha(world.commitment)
            timed("restore", world.restore_root)
            if world.root_information().sha256 != root_sha:
                raise ValueError("single belief world differs from FAIR root")
            with torch.inference_mode():
                policy, logits = timed("inference", self.inference.predict, information)
                root = SearchNode(policy, logits, limits=limits, seed=split_seed(seed, "candidate", root_sha), noise=True)
                nodes = {root_sha: root}
                while completed < budget:
                    check_deadline()
                    timed("restore", world.restore_root)
                    if world.root_information().sha256 != root_sha:
                        raise ValueError("restored FAIR root changed")
                    generator = torch.Generator().manual_seed(split_seed(seed, "opponent", attempts))
                    attempts += 1
                    node, current, history = root, information, root_sha
                    path = []
                    decisions = 0
                    while True:
                        check_deadline()
                        if current.owner != information.owner or current.tick < information.tick:
                            raise ValueError("search node is not a root-owner decision boundary")
                        if decisions >= limits.soft_decisions or current.tick - information.tick >= limits.soft_ticks:
                            leaf = node.value
                            break
                        action = node.select(limits)
                        if not current.legality.legal(action):
                            raise ValueError("search selected an illegal action")
                        transition = timed("transition", world.transition, action, opponent=self.opponent,
                            generator=generator, hard_tick=information.tick + limits.soft_ticks + limits.extension_ticks,
                            maximum_decisions=limits.soft_decisions + limits.extension_decisions - decisions,
                            check_deadline=check_deadline)
                        if (type(transition.completed) is not bool or type(transition.ticks) is not int
                                or transition.ticks < 0 or type(transition.decisions) is not int or transition.decisions < 0):
                            raise ValueError("malformed completed transition evidence")
                        if not transition.completed:
                            if transition.information is not None or transition.terminal_value is not None:
                                raise ValueError("unfinished edge supplied a leaf")
                            if world.root_owner_information().sha256 != current.sha256:
                                raise ValueError("unfinished transition did not restore its prior boundary")
                            discarded += 1
                            leaf = node.value
                            break
                        if transition.ticks < 1 or transition.decisions < 1:
                            raise ValueError("completed edge did not advance")
                        if transition.reason not in ("executed", "preempted", "rejected", "wait_expired", "terminal_expired"):
                            raise ValueError("completed edge has no lifecycle evidence")
                        path.append((node, action))
                        lifecycle[transition.reason] = lifecycle.get(transition.reason, 0) + 1
                        decisions += transition.decisions
                        if transition.terminal_value is not None:
                            if transition.information is not None or transition.terminal_value not in (-1., 0., 1.):
                                raise ValueError("invalid terminal value")
                            leaf = transition.terminal_value
                            break
                        previous = current
                        current = transition.information
                        if (current is None or current.owner != information.owner
                                or current.tick != previous.tick + transition.ticks):
                            raise ValueError("transition boundary or player perspective differs")
                        history = semantic_digest([history, action.key, current.sha256])
                        if history not in nodes:
                            policy, logits = timed("inference", self.inference.predict, current)
                            nodes[history] = SearchNode(policy, logits, limits=limits,
                                seed=split_seed(seed, "candidate", history))
                            leaf = nodes[history].value
                            break
                        node = nodes[history]
                    check_deadline()
                    if not path:
                        invalid += 1
                        if invalid >= limits.maximum_invalid_attempts:
                            raise ValueError("root quarantined after bounded incomplete attempts")
                        continue
                    if not math.isfinite(leaf) or not -1 <= leaf <= 1:
                        raise ValueError("invalid root-player leaf value")
                    for parent, action in path:
                        parent.visits[action.key] += 1
                        parent.totals[action.key] += leaf
                    completed += 1
                candidates = [dict(action_id=codec.encode(entry.action), proposal=entry.proposal,
                    original_joint_logp=entry.raw_logp, prior_projected=entry.prior, prior_search=root.priors[entry.action.key],
                    prior_card=entry.card_mass, visits=root.visits[entry.action.key],
                    q=root.totals[entry.action.key] / root.visits[entry.action.key] if root.visits[entry.action.key] else 0.)
                    for entry in root.candidates]
                result = dict(schema=SEARCH_SCHEMA, root_fair_state_sha=root_sha, root_player=information.owner,
                    value_perspective=information.owner, world_sampler_sha=self.belief.sampler_sha,
                    world_commitment=world.commitment, search_protocol_sha=limits.sha256,
                    opponent_controller_sha=self.opponent.controller_sha, valid_simulations=completed,
                    invalid_attempts=invalid, unfinished_edge_backups=0, candidates=candidates,
                    mode_mass=root.mode_mass, noise_fraction=limits.root_noise)
                visits = validate_search(result, info=information, codec=codec, identity=identity, budget=budget)
                positive = sorted(action_id for action_id, count in visits.items() if count)
                selected = random.Random(split_seed(behavior_seed, "behavior")).choices(positive,
                    weights=[visits[action_id] for action_id in positive], k=1)[0]
                target = [[action_id, visits[action_id]] for action_id in positive] if mode == "FULL" else None
                return SearchOutcome(result, codec.decode(selected), target, dict(seconds=time.perf_counter()-started,
                    timings=timings, attempts=attempts, nodes=len(nodes), discarded_edges=discarded,
                    completed_lifecycle=lifecycle, world_manifest=world.manifest))
        finally:
            if world is not None:
                world.close()
