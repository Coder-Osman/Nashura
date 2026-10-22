"""PUCT contracts for the Hog specialist; native state comes from an adapter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from copy import deepcopy
import hashlib
import json
import math
import random
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

import torch

from recorder_capsule import SearchRecorderState
from packed_action import PackedActionSpace, PackedPrior

STATE_VERSION = "tree-search-state.v1"


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        import orjson
        return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS)
    except ImportError:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode()


def finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class SearchState:
    native_snapshot: Mapping[str, Any]
    observation: Mapping[str, Any]
    actor: int
    legal_actions: tuple[str, ...]
    external_queue: tuple[Mapping[str, Any], ...]
    reward_ledger: Mapping[str, Any]
    event_cursor: Mapping[str, Any]
    policy_state: Mapping[str, Any]
    terminal: bool = False
    action_space: PackedActionSpace | None = None

    def __post_init__(self) -> None:
        if self.actor not in (0, 1):
            raise ValueError("actor must be 0 or 1")
        if self.action_space is not None:
            if self.legal_actions and tuple(self.action_space.canonical_keys()) != tuple(self.legal_actions):
                raise ValueError("packed action space differs from legal action keys")
            if not self.terminal and not len(self.action_space):
                raise ValueError("nonterminal state needs a packed action")
        elif not self.terminal and not self.legal_actions:
            raise ValueError("nonterminal state needs a legal action")
        if self.action_space is None and len(set(self.legal_actions)) != len(self.legal_actions):
            raise ValueError("duplicate legal action")

    def to_dict(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SearchState":
        if raw.get("version") != STATE_VERSION:
            raise ValueError("unsupported tree search state version")
        expected = {"version", *cls.__dataclass_fields__}
        if set(raw) not in (expected, expected - {"action_space"}):
            raise ValueError("tree search state fields do not match the versioned contract")
        return cls(
            native_snapshot=raw["native_snapshot"], observation=raw["observation"],
            actor=int(raw["actor"]), legal_actions=tuple(raw["legal_actions"]),
            external_queue=tuple(raw["external_queue"]), reward_ledger=raw["reward_ledger"],
            event_cursor=raw["event_cursor"], policy_state=raw["policy_state"],
            terminal=bool(raw["terminal"]),
            action_space=raw.get("action_space"),
        )


@dataclass(frozen=True)
class SearchView:
    """Non-restorable view of a search position.

    A view contains the complete semantic state needed by PUCT, but no native
    handle.  Restoring a position is an explicit adapter operation and is
    represented by a separate checkpoint type in ``native_adapter``.
    """

    observation: Mapping[str, Any]
    actor: int
    legal_actions: tuple[str, ...]
    external_queue: tuple[Mapping[str, Any], ...]
    reward_ledger: Mapping[str, Any]
    event_cursor: Mapping[str, Any]
    policy_state: Mapping[str, Any]
    terminal: bool = False
    execution_digest: str | None = None
    action_space: PackedActionSpace | None = None

    def __post_init__(self) -> None:
        if self.actor not in (0, 1):
            raise ValueError("actor must be 0 or 1")
        if self.action_space is not None:
            if self.legal_actions and tuple(self.action_space.canonical_keys()) != tuple(self.legal_actions):
                raise ValueError("packed action space differs from legal action keys")
            if not self.terminal and not len(self.action_space):
                raise ValueError("nonterminal view needs a packed action")
        elif not self.terminal and not self.legal_actions:
            raise ValueError("nonterminal view needs a legal action")
        if self.action_space is None and len(set(self.legal_actions)) != len(self.legal_actions):
            raise ValueError("duplicate legal action")

    @classmethod
    def from_state(cls, state: SearchState, *, execution_digest: str | None = None) -> "SearchView":
        return cls(
            observation=state.observation,
            actor=state.actor,
            legal_actions=state.legal_actions,
            external_queue=state.external_queue,
            reward_ledger=state.reward_ledger,
            event_cursor=state.event_cursor,
            policy_state=state.policy_state,
            terminal=state.terminal,
            execution_digest=execution_digest,
            action_space=state.action_space,
        )

    def to_dict(self) -> dict[str, Any]:
        # An explicit empty value makes the existing semantic digest usable,
        # while keeping the absence of a recoverable handle visible.
        return {
            "version": STATE_VERSION,
            "native_snapshot": {},
            "observation": self.observation,
            "actor": self.actor,
            "legal_actions": self.legal_actions,
            "external_queue": self.external_queue,
            "reward_ledger": self.reward_ledger,
            "event_cursor": self.event_cursor,
            "policy_state": self.policy_state,
            "terminal": self.terminal,
        }


@dataclass(frozen=True)
class Transition:
    state: SearchState | SearchView | None
    reward: float
    decision_points: int
    native_seconds: float

    def __post_init__(self) -> None:
        finite(self.reward, "transition reward")
        if type(self.decision_points) is not int or self.decision_points < 1:
            raise ValueError("transition must advance at least one decision point")
        if finite(self.native_seconds, "native seconds") < 0:
            raise ValueError("native seconds must be non-negative")


class NativeTreeSearchAdapter(Protocol):
    def save_search_state(self) -> SearchState: ...
    def restore_search_state(self, state: SearchState) -> None: ...
    def apply_action(self, action: str) -> None: ...
    def advance_until_decision(self, max_decision_points: int, max_native_seconds: float) -> Transition: ...
    def fingerprint(self) -> str: ...

    # Optimized adapters may expose these methods.  The search keeps the
    # legacy methods as a compatibility path for deterministic test adapters.
    def checkpoint_restorable(self) -> Any: ...
    def restore(self, checkpoint: Any) -> None: ...
    def release(self, checkpoint: Any) -> None: ...
    def capture_current_view(self) -> SearchView: ...
    def advance_current_until_decision(self, max_decision_points: int,
                                       max_native_seconds: float) -> Transition: ...
    def advance_current_without_view(self, max_decision_points: int,
                                     max_native_seconds: float) -> Transition: ...


def normalize(prior: Mapping[str, float], actions: Sequence[str], *,
              temperature: float = 1.0, top_k: int | None = None) -> dict[str, float]:
    if temperature <= 0 or not math.isfinite(temperature):
        raise ValueError("temperature must be positive")
    if not actions:
        return {}
    if set(prior) - set(actions):
        raise ValueError("prior contains an illegal action")
    if any(finite(prior.get(action, 0.0), "prior") < 0 for action in actions):
        raise ValueError("prior must be non-negative")
    ranked = sorted(actions, key=lambda action: (-prior.get(action, 0.0), action))
    chosen = ranked[:top_k] if top_k is not None else ranked
    powered = {action: prior.get(action, 0.0) ** (1.0 / temperature) for action in chosen}
    total = sum(powered.values())
    return ({action: weight / total for action, weight in powered.items()} if total else
            {action: 1.0 / len(chosen) for action in chosen})


def normalize_packed(prior: Mapping[int, float], actions: PackedActionSpace, *,
                     temperature: float = 1.0, top_k: int | None = None) -> dict[int, float]:
    """Normalize numeric prior mass without constructing action strings."""
    if temperature <= 0 or not actions:
        raise ValueError("packed prior requires a non-empty positive temperature action space")
    if set(prior) - set(range(len(actions))):
        raise ValueError("packed prior contains an invalid action index")
    ranked = sorted(range(len(actions)), key=lambda index: (
        -float(prior.get(index, 0.0)), actions.canonical_tie_rank[index]))
    chosen = ranked if top_k is None else ranked[:int(top_k)]
    powered = {index: float(prior.get(index, 0.0)) ** (1.0 / temperature)
               for index in chosen}
    total = sum(powered.values())
    if total:
        return {index: value / total for index, value in powered.items()}
    weight = 1.0 / len(chosen)
    return {index: weight for index in chosen}


@dataclass
class SearchNode:
    state: SearchState | SearchView
    prior: Mapping[str, float] | PackedPrior | None
    expanded: bool = True
    visits: dict[Any, int] = field(default_factory=dict)
    value_sum: dict[Any, float] = field(default_factory=dict)
    children: dict[tuple[Any, str], "SearchNode"] = field(default_factory=dict)
    edges: dict[Any, "SearchEdgeRecord"] = field(default_factory=dict)
    action_space: PackedActionSpace | None = field(default=None, init=False)
    packed_prior: PackedPrior | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.expanded:
            if self.state.terminal:
                raise ValueError("terminal node cannot be lazy")
            if self.prior:
                raise ValueError("lazy node cannot carry a prior")
            return
        if self.prior is None:
            raise ValueError("expanded node needs a prior")
        if isinstance(self.prior, PackedPrior):
            self.action_space = self.prior.actions
            self.packed_prior = self.prior
            if self.state.action_space is not None and self.state.action_space != self.action_space:
                raise ValueError("node packed action space differs from state")
        elif set(self.prior) - set(self.state.legal_actions):
            raise ValueError("node prior contains an illegal action")
        if not self.state.terminal and not self.prior:
            raise ValueError("nonterminal node needs a candidate action")
        if self.action_space is None:
            self.prior = normalize(self.prior, tuple(self.prior))
            self.visits = {action: 0 for action in self.prior}
            self.value_sum = {action: 0.0 for action in self.prior}

    @property
    def total_visits(self) -> int:
        return sum(self.visits.values())

    def q(self, action: Any) -> float:
        visits = self.visits.get(action, 0)
        return self.value_sum.get(action, 0.0) / visits if visits else 0.0

    def has_action(self, action: Any) -> bool:
        if not self.expanded:
            raise ValueError("lazy node must be expanded before selecting an action")
        if self.action_space is not None:
            return (isinstance(action, int) and 0 <= action < len(self.action_space)
                    and float(self.packed_prior.mass[action]) > 0)
        return action in self.prior

    def prior_mass(self, action: Any) -> float:
        if not self.expanded:
            raise ValueError("lazy node must be expanded before reading its prior")
        if self.action_space is not None:
            return float(self.packed_prior.mass[action])
        return float(self.prior[action])

    def sample_prior(self, rng: random.Random) -> Any:
        if not self.expanded:
            raise ValueError("lazy node must be expanded before sampling an action")
        if self.action_space is None:
            return rng.choices(list(self.prior), weights=list(self.prior.values()))[0]
        indices = [int(index) for index in torch.nonzero(self.packed_prior.mass > 0,
                                                         as_tuple=False).flatten()]
        weights = [float(self.packed_prior.mass[index]) for index in indices]
        return rng.choices(indices, weights=weights)[0]

    def select(self, cpuct: float) -> Any:
        if not self.expanded:
            raise ValueError("lazy node must be expanded before selecting an action")
        total = math.sqrt(max(1, self.total_visits))
        if self.action_space is not None:
            best = None
            best_score = None
            for action in self.visits:
                score = self.q(action) + cpuct * self.prior_mass(action) * total / (1 + self.visits[action])
                rank = -self.action_space.canonical_tie_rank[action]
                candidate = (score, self.prior_mass(action), rank)
                if best_score is None or candidate > best_score:
                    best, best_score = action, candidate
            mask = self.packed_prior.mass > 0
            if self.visits:
                visited = torch.tensor(tuple(self.visits), dtype=torch.long, device=mask.device)
                mask = mask.clone()
                mask[visited] = False
            if bool(mask.any()):
                unvisited_mass = self.packed_prior.mass.masked_fill(~mask, float("-inf"))
                unvisited = int(torch.argmax(unvisited_mass))
                candidate = (cpuct * float(self.packed_prior.mass[unvisited]) * total,
                             float(self.packed_prior.mass[unvisited]),
                             -self.action_space.canonical_tie_rank[unvisited])
                if best_score is None or candidate > best_score:
                    return unvisited
            if best is None:
                raise ValueError("packed node has no selectable action")
            return best
        return max(self.prior, key=lambda action: (
            self.q(action) + cpuct * self.prior[action] * total / (1 + self.visits[action]),
            self.prior[action],
            (-self.action_space.canonical_tie_rank[action]
             if self.action_space is not None else action)))

    def policy(self, temperature: float = 1.0) -> dict[str, float]:
        if not self.expanded:
            return {}
        if self.action_space is not None:
            decoded = {self.action_space.decode_one(action): self.visits.get(action, 0)
                       for action in self.visits}
            total = sum(decoded.values())
            return ({action: count / total for action, count in decoded.items()}
                    if total else {})
        if not self.state.legal_actions:
            return {}
        if temperature <= 0:
            best = max(self.prior, key=lambda action: (
                self.visits[action],
                (-self.action_space.canonical_tie_rank[action]
                 if self.action_space is not None else action)))
            return {action: float(action == best) for action in self.state.legal_actions}
        weights = {action: self.visits.get(action, 0) ** (1.0 / temperature)
                   for action in self.state.legal_actions}
        total = sum(weights.values())
        return ({action: weights[action] / total for action in weights} if total else
                {action: self.prior.get(action, 0.0) for action in self.state.legal_actions})


@dataclass
class SearchEdgeRecord:
    action_id: str
    child: SearchNode
    reward: float
    discount: float
    decision_points: int
    budget_cost: float
    child_semantic_key: str
    child_component_hashes: dict[str, str]
    child_audit_values: dict[str, Any]
    replay_count: int = 0


class DeterministicEdgeMismatch(ValueError):
    pass


@dataclass(frozen=True)
class OpponentResponse:
    action: str
    probability: float
    value: float


def aggregate_opponent_values(responses: Sequence[OpponentResponse]) -> tuple[float, float]:
    """Expected value and worst positive-probability Top-4 reply, from learner perspective."""
    if not responses:
        raise ValueError("opponent response set is empty")
    if any(r.probability < 0 or not math.isfinite(r.probability) for r in responses):
        raise ValueError("opponent probabilities must be finite and non-negative")
    total = sum(r.probability for r in responses)
    if total <= 0:
        raise ValueError("opponent response probabilities have zero mass")
    expected = sum(r.probability * finite(r.value, "response value") for r in responses) / total
    robust = min(r.value for r in responses if r.probability > 0)
    return expected, robust


def frozen_opponent_target(prior: Mapping[str, float], legal_actions: Sequence[str],
                           reply_values: Mapping[str, float], *, top_k: int = 4,
                           temperature: float = 0.5) -> tuple[float, float, float]:
    """Use every frozen-policy Top-K reply; never infer a missing reply's value."""
    probabilities = normalize(prior, legal_actions, top_k=top_k, temperature=temperature)
    if not probabilities or set(reply_values) != set(probabilities):
        raise ValueError("all and only frozen opponent Top-K replies need evaluated values")
    expected, robust = aggregate_opponent_values([
        OpponentResponse(action, probability, finite(reply_values[action], "reply value"))
        for action, probability in probabilities.items()
    ])
    return expected, robust, q_target(expected, robust)


def q_target(q_expected: float, q_robust: float) -> float:
    return 0.7 * finite(q_expected, "q_expected") + 0.3 * finite(q_robust, "q_robust")


class PuctSearch:
    def __init__(self, adapter: NativeTreeSearchAdapter, policy_prior: Callable[[SearchState], Mapping[str, float]],
                 opponent_prior: Callable[[SearchState], Mapping[str, float]],
                 evaluate: Callable[[SearchState], float], *, cpuct: float = 1.5, gamma: float = 0.999,
                 max_decision_points: int = 6, max_native_seconds: float = 10.0,
                 root_noise: bool = False, dirichlet_alpha: float = 0.25,
                 dirichlet_epsilon: float = 0.25, opponent_top_k: int = 4,
                 opponent_temperature: float = 0.5, seed: int = 0,
                 state_key: Callable[[SearchState], str] | None = None,
                 compact_children: bool = False,
                 root_selector: Callable[[SearchNode], str] | None = None,
                 transition_discount: Callable[[SearchState, Transition], float] | None = None,
                 edge_memo: bool = False, edge_verify_every: int = 1,
                 edge_component_diagnostics: bool = False,
                 deadline: float | None = None):
        if (cpuct <= 0 or not 0 < gamma <= 1 or max_decision_points < 1 or
                max_native_seconds <= 0 or dirichlet_alpha <= 0 or not 0 <= dirichlet_epsilon <= 1 or
                opponent_top_k < 1 or edge_verify_every < 0):
            raise ValueError("invalid PUCT limits")
        self.adapter, self.policy_prior, self.opponent_prior, self.evaluate = (
            adapter, policy_prior, opponent_prior, evaluate)
        self.cpuct, self.gamma = cpuct, gamma
        self.max_decision_points, self.max_native_seconds = max_decision_points, max_native_seconds
        self.root_noise, self.dirichlet_alpha, self.dirichlet_epsilon = (
            root_noise, dirichlet_alpha, dirichlet_epsilon)
        self.opponent_top_k, self.opponent_temperature, self.seed = (
            opponent_top_k, opponent_temperature, seed)
        self.state_key = state_key or (
            semantic_state_key_v2 if edge_memo else state_digest
        )
        self.compact_children = compact_children
        self.root_selector = root_selector
        self.transition_discount = transition_discount
        self.edge_memo = bool(edge_memo)
        self.edge_verify_every = int(edge_verify_every)
        self.edge_component_diagnostics = bool(edge_component_diagnostics)
        self._edge_replay_ordinal = 0
        self.deadline = deadline

    def _discount(self, source: SearchState, transition: Transition) -> float:
        value = (self.gamma ** transition.decision_points if self.transition_discount is None
                 else finite(self.transition_discount(source, transition), "transition discount"))
        if not 0 <= value <= 1:
            raise ValueError("transition discount must be in [0, 1]")
        return value

    def _profiled(self, name: str, callback: Callable[..., Any], *args: Any) -> Any:
        add_profile = getattr(self.adapter, "_add_profile", None)
        if add_profile is None or not getattr(self.adapter, "profile_enabled", False):
            return callback(*args)
        started = time.perf_counter()
        try:
            return callback(*args)
        finally:
            add_profile(name, started)

    def _count(self, name: str, amount: int = 1) -> None:
        if not getattr(self.adapter, "profile_enabled", False):
            return
        count_profile = getattr(self.adapter, "_count_profile", None)
        if count_profile is not None:
            count_profile(name, amount)

    def _set_max(self, name: str, value: int) -> None:
        if not getattr(self.adapter, "profile_enabled", False):
            return
        max_profile = getattr(self.adapter, "_max_profile", None)
        if max_profile is not None:
            max_profile(name, value)

    def _node(self, state: SearchState | SearchView, root_actor: int) -> SearchNode:
        if state.terminal:
            return SearchNode(state, {})
        action_count = len(state.action_space) if state.action_space is not None else len(state.legal_actions)
        self._count("legal_action_count_sum", action_count)
        if state.action_space is None:
            self._count("canonical_action_string_count", action_count)
        if state.actor == root_actor:
            self._count("policy_prior_calls")
            prior = self._profiled("policy_prior_ms", self.policy_prior, state)
        else:
            self._count("opponent_prior_calls")
            prior = self._profiled("opponent_prior_ms", self.opponent_prior, state)
        self._count("prior_action_materialized_count", len(prior.mass)
                    if isinstance(prior, PackedPrior) else len(prior))
        if isinstance(prior, PackedPrior):
            if state.action_space is None:
                raise ValueError("packed prior requires a packed state action space")
            return SearchNode(
                state,
                prior.normalized(
                    top_k=None if state.actor == root_actor else self.opponent_top_k,
                    temperature=1.0 if state.actor == root_actor else self.opponent_temperature,
                ),
            )
        return SearchNode(state, normalize(
            prior, state.legal_actions,
            top_k=None if state.actor == root_actor else self.opponent_top_k,
            temperature=1.0 if state.actor == root_actor else self.opponent_temperature))

    def _expand_lazy_node(self, node: SearchNode, root_actor: int) -> None:
        if node.expanded:
            return
        expanded = self._node(node.state, root_actor)
        node.prior = expanded.prior
        node.visits = expanded.visits
        node.value_sum = expanded.value_sum
        node.action_space = expanded.action_space
        node.packed_prior = expanded.packed_prior
        node.expanded = True

    def run(self, simulations: int, *, root_state: SearchState | SearchView | None = None,
            root_checkpoint: Any | None = None) -> SearchNode:
        if simulations < 1:
            raise ValueError("simulations must be positive")
        optimized = root_checkpoint is not None or all(
            hasattr(self.adapter, name) for name in
            ("checkpoint_restorable", "restore", "capture_current_view",
             "advance_current_until_decision", "release")
        )
        owns_root_checkpoint = False
        if root_checkpoint is not None:
            if root_state is None:
                root_state = self.adapter.capture_current_view()
        elif optimized and root_state is None:
            root_checkpoint = self.adapter.checkpoint_restorable()
            owns_root_checkpoint = True
            try:
                root_state = self.adapter.capture_current_view()
            except BaseException:
                self.adapter.release(root_checkpoint)
                raise
        elif root_state is None:
            root_state = self.adapter.save_search_state()
        else:
            # A supplied root may have just had a child released.  Make the
            # search entry point self-contained instead of requiring callers
            # to remember which native world is currently active.
            if optimized:
                raise ValueError("optimized PUCT requires an explicit root checkpoint")
            self.adapter.restore_search_state(root_state)
        root: SearchNode | None = None
        try:
            if root_state.terminal:
                raise ValueError("root is terminal")
            root = self._node(root_state, root_state.actor)
            rng = random.Random(self.seed)
            if self.root_noise and root.action_space is not None:
                raise ValueError("packed root noise path is not enabled in this candidate")
            if self.root_noise:
                draws = [rng.gammavariate(self.dirichlet_alpha, 1.0) for _ in root.prior]
                total = sum(draws)
                root.prior = {action: (1 - self.dirichlet_epsilon) * root.prior[action]
                              + self.dirichlet_epsilon * draw / total
                              for action, draw in zip(root.prior, draws)}
            root_fingerprint = self._profiled("fingerprint_ms", self.adapter.fingerprint)
            for simulation in range(simulations):
                set_profile_context = getattr(self.adapter, "set_profile_context", None)
                if set_profile_context is not None:
                    set_profile_context(simulation_index=simulation, edge_index=0,
                                        depth=0, actor=root_state.actor)
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise TimeoutError("search deadline before full simulation budget")
                if optimized:
                    self.adapter.restore(root_checkpoint)
                else:
                    self.adapter.restore_search_state(root_state)
                node = root
                active_state = root_state
                active_state_is_view = True
                path: list[tuple[SearchNode, str, float, float]] = []
                decisions, seconds = 0, 0.0
                while True:
                    if set_profile_context is not None:
                        set_profile_context(simulation_index=simulation,
                                            edge_index=len(path), depth=len(path),
                                            actor=node.state.actor)
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        raise TimeoutError("search deadline before full simulation budget")
                    if node.state.terminal:
                        value = 0.0
                        break
                    if decisions >= self.max_decision_points or seconds >= self.max_native_seconds:
                        if not active_state_is_view:
                            active_state = self.adapter.capture_current_view()
                            active_state_is_view = True
                        value = self._evaluate(active_state)
                        break
                    if not node.expanded:
                        self._expand_lazy_node(node, root_state.actor)
                    action = (self.root_selector(node) if node is root and self.root_selector is not None else
                              node.select(self.cpuct) if node.state.actor == root_state.actor else
                              node.sample_prior(rng))
                    if not node.has_action(action):
                        raise ValueError("search selector returned an illegal candidate")
                    self._count("edge_traversals")
                    self._set_max("max_search_depth", len(path) + 1)
                    source_state = active_state
                    self._count("search_recorder_decide_calls")
                    self._profiled("apply_action_ms", self.adapter.apply_action, action)
                    edge_record = node.edges.get(action) if self.edge_memo else None
                    if edge_record is not None:
                        self._edge_replay_ordinal += 1
                        verify = (self.edge_verify_every == 1 or
                                  (self.edge_verify_every > 1 and
                                   self._edge_replay_ordinal % self.edge_verify_every == 0))
                        advance_viewless = getattr(
                            self.adapter, "advance_current_without_view", None)
                        if verify or advance_viewless is None:
                            advance_with_view = (
                                self.adapter.advance_current_until_decision if optimized else
                                self.adapter.advance_until_decision
                            )
                            transition = advance_with_view(
                                self.max_decision_points - decisions,
                                self.max_native_seconds - seconds)
                        else:
                            transition = advance_viewless(
                                self.max_decision_points - decisions,
                                self.max_native_seconds - seconds)
                        if (not math.isclose(transition.reward, edge_record.reward,
                                             rel_tol=0.0, abs_tol=1e-9) or
                                transition.decision_points != edge_record.decision_points):
                            self._count("edge_memo_mismatches")
                            raise DeterministicEdgeMismatch(
                                f"known edge {action!r} changed reward or decision count")
                        if verify and transition.state is not None:
                            self._count("state_key_calls")
                            self._count("state_key_existing_edge_calls")
                            actual_key = self._profiled(
                                "state_key_ms", self.state_key, transition.state)
                            if actual_key != edge_record.child_semantic_key:
                                self._count("edge_memo_mismatches")
                                actual_components = (
                                    semantic_component_hashes(transition.state)
                                    if self.edge_component_diagnostics else {})
                                changed = sorted(
                                    name for name in set(edge_record.child_component_hashes) |
                                    set(actual_components)
                                    if edge_record.child_component_hashes.get(name) !=
                                    actual_components.get(name)
                                )
                                raise DeterministicEdgeMismatch(
                                    f"known edge {action!r} changed semantic child state: "
                                    f"{','.join(changed)}; expected="
                                    f"{json.dumps(edge_record.child_audit_values, sort_keys=True)}; "
                                    f"actual={json.dumps(edge_replay_audit_values(transition.state), sort_keys=True)}")
                            active_state = transition.state
                            active_state_is_view = True
                            self._count("edge_memo_verifications")
                        else:
                            active_state = edge_record.child.state
                            active_state_is_view = False
                            self._count("edge_memo_viewless_replays")
                        edge_record.replay_count += 1
                        self._count("edge_memo_known_replays")
                        decisions += edge_record.decision_points
                        seconds += edge_record.budget_cost
                        if (decisions > self.max_decision_points or
                                seconds > self.max_native_seconds + 1e-9):
                            raise ValueError("cached edge exceeded the simulation horizon")
                        path.append((node, action, edge_record.reward, edge_record.discount))
                        self._count("edge_replays")
                        node = edge_record.child
                        continue
                    transition = (self.adapter.advance_current_until_decision(
                        self.max_decision_points - decisions, self.max_native_seconds - seconds)
                        if optimized else self.adapter.advance_until_decision(
                            self.max_decision_points - decisions, self.max_native_seconds - seconds))
                    if (transition.decision_points > self.max_decision_points - decisions or
                            transition.native_seconds > self.max_native_seconds - seconds + 1e-9):
                        raise ValueError("native transition exceeded the simulation horizon")
                    if transition.state is None:
                        raise ValueError("new edge transition must capture its search view")
                    active_state = transition.state
                    active_state_is_view = True
                    decisions += transition.decision_points
                    seconds += transition.native_seconds
                    discount = self._discount(source_state, transition)
                    path.append((node, action, transition.reward, discount))
                    if (not transition.state.terminal
                            and (decisions >= self.max_decision_points
                                 or seconds >= self.max_native_seconds)):
                        value = (0.0 if transition.state.terminal else
                                 self._evaluate(transition.state))
                        break
                    self._count("state_key_calls")
                    key = (action, self._profiled("state_key_ms", self.state_key, transition.state))
                    release = getattr(self.adapter, "release_search_state", None)
                    child = None
                    try:
                        child = node.children.get(key)
                        if child is None:
                            self._count("state_key_new_edge_calls")
                            self._count("edge_first_expansions")
                            child = (SearchNode(transition.state, {}, expanded=False)
                                     if not transition.state.terminal else
                                     SearchNode(transition.state, {}))
                            node.children[key] = child
                            if self.edge_memo:
                                node.edges[action] = SearchEdgeRecord(
                                    action_id=action, child=child, reward=transition.reward,
                                    discount=discount, decision_points=transition.decision_points,
                                    budget_cost=transition.native_seconds,
                                    child_semantic_key=key[1],
                                    child_component_hashes=(
                                        semantic_component_hashes(transition.state)
                                        if self.edge_component_diagnostics else {}),
                                    child_audit_values=edge_replay_audit_values(transition.state),
                                )
                            value = (0.0 if transition.state.terminal else
                                     self._evaluate(transition.state))
                            break
                        self._count("state_key_existing_edge_calls")
                        self._count("edge_replays")
                    finally:
                        # Optimized views have no native resource. Legacy
                        # states retain the old release behavior.
                        if not optimized and release is not None:
                            release(transition.state)
                        if self.compact_children and child is not None:
                            if isinstance(child.state, SearchView):
                                child.state = replace(
                                    child.state,
                                    observation={"tick": child.state.observation.get("tick")},
                                    external_queue=(), reward_ledger={}, event_cursor={}, policy_state={},
                                )
                            elif not optimized:
                                child.state = replace(
                                    child.state,
                                    native_snapshot={},
                                    observation={"tick": child.state.observation.get("tick")},
                                    external_queue=(), reward_ledger={}, event_cursor={}, policy_state={},
                                )
                    node = child
                for parent, action, reward, discount in reversed(path):
                    value = reward + discount * value
                    parent.visits[action] = parent.visits.get(action, 0) + 1
                    parent.value_sum[action] = parent.value_sum.get(action, 0.0) + value
                self._count("sum_search_depth", len(path))
                self._count("simulation_count")
            if optimized:
                self.adapter.restore(root_checkpoint)
            else:
                self.adapter.restore_search_state(root_state)
            if self._profiled("fingerprint_ms", self.adapter.fingerprint) != root_fingerprint:
                raise ValueError("root restore fingerprint mismatch after PUCT")
            if owns_root_checkpoint:
                self.adapter.release(root_checkpoint)
        except BaseException:
            try:
                if optimized:
                    self.adapter.restore(root_checkpoint)
                else:
                    self.adapter.restore_search_state(root_state)
            finally:
                if owns_root_checkpoint:
                    self.adapter.release(root_checkpoint)
                if root is None and not optimized:
                    release = getattr(self.adapter, "release_search_state", None)
                    if release is not None:
                        release(root_state)
                elif not optimized:
                    self.release(root)
            raise
        assert root is not None
        return root

    def _evaluate(self, state: SearchState | SearchView) -> float:
        self._count("leaf_value_calls")
        return finite(self._profiled("leaf_value_ms", self.evaluate, state), "leaf value")

    def release(self, root: SearchNode) -> None:
        release = getattr(self.adapter, "release_search_state", None)
        if release is None:
            return
        pending = [root]
        seen: set[int] = set()
        while pending:
            node = pending.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            pending.extend(node.children.values())
            if isinstance(node.state, SearchState):
                release(node.state)

    def evaluate_root_replies(self, root: SearchNode, action: str) -> tuple[float, float, float]:
        """Value a root action against every Top-K frozen reply in the full state."""
        evidence = self.evaluate_root_reply_details(root, action)
        expected, robust = aggregate_opponent_values([
            OpponentResponse(key, probability, evidence["reply_values"][key])
            for key, probability in evidence["reply_probabilities"].items()
        ])
        return expected, robust, q_target(expected, robust)

    def evaluate_root_reply_details(self, root: SearchNode, action: str) -> dict[str, Any]:
        """Retain every evaluated frozen reply so the teacher value is auditable."""
        if action not in root.prior or root.state.terminal:
            raise ValueError("root action is not a legal search candidate")
        release = getattr(self.adapter, "release_search_state", None)
        self.adapter.restore_search_state(root.state)
        root_fingerprint = self.adapter.fingerprint()
        try:
            self.adapter.apply_action(action)
            first = self.adapter.advance_until_decision(
                self.max_decision_points, self.max_native_seconds)
            try:
                if (first.decision_points > self.max_decision_points or
                        first.native_seconds > self.max_native_seconds + 1e-9):
                    raise ValueError("root transition exceeded the search horizon")
                reply = first.state
                if reply.terminal or reply.actor == root.state.actor:
                    raise ValueError("root branch did not reach an opponent decision")
                remaining_points = self.max_decision_points - first.decision_points
                remaining_seconds = self.max_native_seconds - first.native_seconds
                if remaining_points < 1 or remaining_seconds <= 0:
                    raise ValueError("opponent response falls outside the search horizon")
                prior = self.opponent_prior(reply)
                probabilities = normalize(prior, reply.legal_actions,
                                          top_k=self.opponent_top_k,
                                          temperature=self.opponent_temperature)
                values = {}
                for response in probabilities:
                    self.adapter.restore_search_state(reply)
                    self.adapter.apply_action(response)
                    second = self.adapter.advance_until_decision(
                        remaining_points, remaining_seconds)
                    try:
                        if (second.decision_points > remaining_points or
                                second.native_seconds > remaining_seconds + 1e-9):
                            raise ValueError("opponent response exceeded the search horizon")
                        leaf = 0.0 if second.state.terminal else finite(self.evaluate(second.state), "reply leaf")
                        values[response] = first.reward + self._discount(root.state, first) * (
                            second.reward + self._discount(reply, second) * leaf)
                    finally:
                        if release is not None:
                            release(second.state)
                frozen_opponent_target(
                    prior, reply.legal_actions, values,
                    top_k=self.opponent_top_k, temperature=self.opponent_temperature)
                return {
                    "version": "frozen-opponent-top-k.v1",
                    "root_action": action,
                    "root_owner": root.state.actor,
                    "opponent_owner": reply.actor,
                    "top_k": self.opponent_top_k,
                    "temperature": self.opponent_temperature,
                    "reply_legal_action_count": len(reply.legal_actions),
                    "reply_state_sha256": state_digest(reply),
                    "frozen_prior_sha256": hashlib.sha256(json.dumps(
                        dict(prior), sort_keys=True, separators=(",", ":"),
                        allow_nan=False).encode()).hexdigest(),
                    "reply_probabilities": probabilities,
                    "reply_values": values,
                }
            finally:
                if release is not None:
                    release(first.state)
        finally:
            self.adapter.restore_search_state(root.state)
            if self.adapter.fingerprint() != root_fingerprint:
                raise ValueError("root restore fingerprint mismatch after reply evaluation")


def state_digest(state: SearchState | SearchView) -> str:
    def compact_recorder(value: Any) -> Any:
        if isinstance(value, SearchRecorderState):
            return {"search_recorder": value.semantic_identity()}
        if isinstance(value, Mapping):
            return {key: compact_recorder(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [compact_recorder(item) for item in value]
        return value

    body = {
        "version": STATE_VERSION,
        "native_snapshot": state.native_snapshot if isinstance(state, SearchState) else {},
        "observation": state.observation,
        "actor": state.actor,
        "legal_actions": (
            {
                "packed_action_space": {
                    name: getattr(state.action_space, name)
                    for name in (
                        "kind", "card_row", "cell", "delay",
                        "ability_source", "canonical_tie_rank",
                    )
                }
            }
            if state.action_space is not None else state.legal_actions
        ),
        "external_queue": state.external_queue,
        "reward_ledger": state.reward_ledger,
        "event_cursor": deepcopy(state.event_cursor),
        "policy_state": compact_recorder(state.policy_state),
        "terminal": state.terminal,
    }
    if isinstance(state, SearchView):
        body["execution_digest"] = state.execution_digest
    def without_operational_handles(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: without_operational_handles(item) for key, item in value.items()
                    if key not in ("handle", "snapshotHandles", "stateEpoch")}
        if isinstance(value, list):
            return [without_operational_handles(item) for item in value]
        if isinstance(value, tuple):
            return [without_operational_handles(item) for item in value]
        return value
    body["native_snapshot"] = without_operational_handles(
        getattr(state, "native_snapshot", {}))

    def jsonable(value: Any) -> Any:
        if hasattr(value, "detach") and hasattr(value, "tolist") and hasattr(value, "shape"):
            detached = value.detach().cpu().contiguous()
            if detached.is_floating_point():
                import torch
                if not bool(torch.isfinite(detached).all()):
                    raise ValueError("state digest rejects non-finite tensors")
            import torch
            raw = detached.view(torch.uint8).numpy().tobytes()
            return {"dtype": str(detached.dtype), "shape": list(detached.shape),
                    "sha256": hashlib.sha256(raw).hexdigest()}
        if isinstance(value, Mapping):
            return {key: jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        return value
    cursor = body["event_cursor"]
    if {"seen", "combat", "public_card_play"} <= set(cursor):
        seen = cursor["seen"]
        if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
            raise ValueError(f"event cursor identities are malformed: {seen!r}")
        parsed = []
        try:
            for item in seen:
                parts = item.split(":")
                if len(parts) == 2:
                    parsed.append(("combat", int(parts[0]), int(parts[1])))
                elif len(parts) >= 3 and parts[0]:
                    parsed.append((":".join(parts[:-2]), int(parts[-2]), int(parts[-1])))
                else:
                    raise ValueError
        except ValueError as exc:
            raise ValueError(f"event cursor identities are malformed: {seen!r}") from exc
        if len({item[1] for item in parsed}) > 1:
            raise ValueError("event cursor spans multiple native epochs")
        ordered = sorted(zip(parsed, seen), key=lambda item: (item[0][0], item[0][2], item[1]))
        if len({item[0] for item in ordered}) != len(ordered):
            raise ValueError("event cursor repeats an identity")
        sequence_gap = None
        combat_sequences = [item[0][2] for item in ordered if item[0][0] == "combat"]
        if combat_sequences:
            sequence_gap = cursor["combat"] - max(combat_sequences)
            if sequence_gap < 1:
                raise ValueError("combat cursor precedes a consumed event")
        canonical_ids = {item: f"event:{index}" for index, (_, item) in enumerate(ordered)}
        cursor["seen"] = sorted(canonical_ids.values())
        cursor["combat"] = sequence_gap

        native_cursors = cursor.get("native")
        if native_cursors is not None:
            if not isinstance(native_cursors, Mapping):
                raise ValueError("native event cursors are malformed")
            for name, value in native_cursors.items():
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise ValueError(f"native event cursor {name} is malformed")
                identity, next_sequence = value
                if identity == "current":
                    if type(next_sequence) is not int:
                        raise ValueError(f"native event cursor {name} sequence is malformed")
                    continue
                if identity is not None:
                    if (not isinstance(identity, (list, tuple)) or len(identity) != 2
                            or any(type(item) is not int for item in identity)):
                        raise ValueError(
                            f"native event cursor {name} identity is malformed: {identity!r}"
                        )
                    native_cursors[name] = ("current", next_sequence)

        def replace_event_ids(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: replace_event_ids(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_event_ids(item) for item in value]
            return canonical_ids.get(value, value) if isinstance(value, str) else value

        body["policy_state"] = replace_event_ids(body["policy_state"])

    fair = body["observation"].get("fair")
    if isinstance(fair, dict):
        groups = {}
        for owner in sorted(fair):
            for entity in sorted(fair[owner].get("entities", []), key=lambda row: row["entity_id"]):
                reference = entity.get("causal_group")
                if isinstance(reference, dict) and "handle" in reference:
                    handle = reference["handle"]
                    groups.setdefault(handle, f"group:{len(groups)}")
        if groups:
            normalized_fair = {}
            for owner, owner_observation in fair.items():
                entities = []
                for entity in owner_observation.get("entities", []):
                    reference = entity.get("causal_group")
                    if isinstance(reference, dict) and "handle" in reference:
                        entity = {**entity, "causal_group": {
                            **reference, "handle": groups[reference["handle"]],
                        }}
                    entities.append(entity)
                normalized_fair[owner] = {**owner_observation, "entities": entities}
            body["observation"] = {**body["observation"], "fair": normalized_fair}
    payload = _canonical_json_bytes(jsonable(body))
    return hashlib.sha256(payload).hexdigest()


def _fast_semantic_state_key_v2(state: SearchState | SearchView) -> str | None:
    """Build the common JSON-only semantic key without deep-copying the view."""
    if not isinstance(state, SearchView):
        return None

    observation = dict(state.observation)
    native_observation = observation.get("native")
    if isinstance(native_observation, dict) and "generation" in native_observation:
        native_observation = dict(native_observation)
        native_observation.pop("generation", None)
        observation["native"] = native_observation

    fair_observations = observation.get("fair")
    if isinstance(fair_observations, dict):
        normalized_fair = {}
        for owner, fair in fair_observations.items():
            if isinstance(fair, dict) and "episode_id" in fair:
                fair = dict(fair)
                fair.pop("episode_id", None)
            normalized_fair[owner] = fair
        observation["fair"] = normalized_fair

    event_cursor = dict(state.event_cursor)
    native_cursors = event_cursor.get("native")
    if isinstance(native_cursors, dict):
        normalized_native = dict(native_cursors)
        for name, value in normalized_native.items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                identity, next_sequence = value
                normalized_native[name] = (
                    "current" if identity is not None else None,
                    0 if type(next_sequence) is int else None,
                )
        event_cursor["native"] = normalized_native

    policy_state = state.policy_state
    if isinstance(policy_state, SearchRecorderState):
        policy_state = {"search_recorder": policy_state.semantic_identity()}
    elif isinstance(policy_state, Mapping):
        policy_state = {
            key: (item.semantic_identity() if isinstance(item, SearchRecorderState) else item)
            for key, item in policy_state.items()
        }

    cursor = event_cursor
    if {"seen", "combat", "public_card_play"} <= set(cursor):
        seen = cursor["seen"]
        if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
            return None
        parsed = []
        try:
            for item in seen:
                parts = item.split(":")
                if len(parts) == 2:
                    parsed.append(("combat", int(parts[0]), int(parts[1])))
                elif len(parts) >= 3 and parts[0]:
                    parsed.append((":".join(parts[:-2]), int(parts[-2]), int(parts[-1])))
                else:
                    return None
        except (TypeError, ValueError):
            return None
        if len({item[1] for item in parsed}) > 1:
            return None
        ordered = sorted(zip(parsed, seen), key=lambda item: (item[0][0], item[0][2], item[1]))
        if len({item[0] for item in ordered}) != len(ordered):
            return None
        sequence_gap = None
        combat_sequences = [item[0][2] for item in ordered if item[0][0] == "combat"]
        if combat_sequences:
            sequence_gap = cursor["combat"] - max(combat_sequences)
            if sequence_gap < 1:
                return None
        canonical_ids = {item: f"event:{index}" for index, (_, item) in enumerate(ordered)}
        cursor["seen"] = sorted(canonical_ids.values())
        cursor["combat"] = sequence_gap

        native_cursors = cursor.get("native")
        if native_cursors is not None:
            if not isinstance(native_cursors, Mapping):
                return None
            for name, value in native_cursors.items():
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    return None
                identity, next_sequence = value
                if identity == "current":
                    if type(next_sequence) is not int:
                        return None
                    continue
                if identity is not None:
                    if (not isinstance(identity, (list, tuple)) or len(identity) != 2
                            or any(type(item) is not int for item in identity)):
                        return None
                    native_cursors[name] = ("current", next_sequence)

        def replace_event_ids(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: replace_event_ids(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_event_ids(item) for item in value]
            return canonical_ids.get(value, value) if isinstance(value, str) else value

        policy_state = replace_event_ids(policy_state)

    fair = observation.get("fair")
    if isinstance(fair, dict):
        groups = {}
        for owner in sorted(fair):
            owner_observation = fair[owner]
            for entity in sorted(owner_observation.get("entities", []),
                                 key=lambda row: row["entity_id"]):
                reference = entity.get("causal_group")
                if isinstance(reference, dict) and "handle" in reference:
                    groups.setdefault(reference["handle"], f"group:{len(groups)}")
        if groups:
            normalized_fair = {}
            for owner, owner_observation in fair.items():
                entities = []
                for entity in owner_observation.get("entities", []):
                    reference = entity.get("causal_group")
                    if isinstance(reference, dict) and "handle" in reference:
                        entity = {**entity, "causal_group": {
                            **reference, "handle": groups[reference["handle"]],
                        }}
                    entities.append(entity)
                normalized_fair[owner] = {**owner_observation, "entities": entities}
            observation["fair"] = normalized_fair

    legal_actions = (
        {"packed_action_space_sha256": state.action_space.semantic_sha256}
        if state.action_space is not None else state.legal_actions
    )
    body = {
        "version": STATE_VERSION,
        "native_snapshot": {},
        "observation": observation,
        "actor": state.actor,
        "legal_actions": legal_actions,
        "external_queue": state.external_queue,
        "reward_ledger": state.reward_ledger,
        "event_cursor": event_cursor,
        "policy_state": policy_state,
        "terminal": state.terminal,
        "execution_digest": state.execution_digest,
    }
    try:
        inner = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return None
    payload = f"semantic-state-key.v2:{inner}".encode()
    return hashlib.sha256(payload).hexdigest()


def semantic_state_key_v2(state: SearchState | SearchView) -> str:
    fast = _fast_semantic_state_key_v2(state)
    if fast is not None:
        return fast
    observation = dict(state.observation)
    native_observation = observation.get("native")
    if isinstance(native_observation, dict):
        native_observation = dict(native_observation)
        native_observation.pop("generation", None)
        observation["native"] = native_observation
    fair_observations = observation.get("fair")
    if isinstance(fair_observations, dict):
        normalized_fair = {}
        for owner, fair in fair_observations.items():
            if isinstance(fair, dict):
                fair = dict(fair)
                fair.pop("episode_id", None)
            normalized_fair[owner] = fair
        observation["fair"] = normalized_fair

    # Only the native cursor entries are rewritten below.  Avoid deep-copying
    # the complete event cursor on every edge replay; its other fields are
    # treated as immutable by the rollout contract.
    event_cursor = dict(state.event_cursor)
    native_cursors = event_cursor.get("native")
    if isinstance(native_cursors, dict):
        native_cursors = dict(native_cursors)
        for name, value in native_cursors.items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                identity, next_sequence = value
                native_cursors[name] = (
                    "current" if identity is not None else None,
                    0 if type(next_sequence) is int else None,
                )
        event_cursor["native"] = native_cursors

    if isinstance(state, SearchState):
        native_snapshot = dict(state.native_snapshot)
        for name in ("generation", "configRevision", "stateEpoch", "handle",
                     "snapshotHandles"):
            native_snapshot.pop(name, None)
        canonical = replace(
            state, observation=observation, event_cursor=event_cursor,
            native_snapshot=native_snapshot,
        )
    else:
        canonical = replace(state, observation=observation, event_cursor=event_cursor)
    payload = f"semantic-state-key.v2:{state_digest(canonical)}".encode()
    return hashlib.sha256(payload).hexdigest()


def semantic_component_hashes(state: SearchState | SearchView) -> dict[str, str]:
    def digest(value: Any) -> str:
        isolated = SearchView(
            observation={"component": value}, actor=0, legal_actions=("wait",),
            external_queue=(), reward_ledger={}, event_cursor={}, policy_state={},
        )
        return state_digest(isolated)

    result: dict[str, str] = {}

    def add(path: str, value: Any, depth: int = 0) -> None:
        if depth < 8 and isinstance(value, Mapping) and value:
            for name, child in value.items():
                add(f"{path}.{name}", child, depth + 1)
        elif depth < 8 and isinstance(value, (list, tuple)) and len(value) <= 32:
            for index, child in enumerate(value):
                add(f"{path}.{index}", child, depth + 1)
        else:
            result[path] = digest(value)

    for name, value in state.observation.items():
        add(f"observation.{name}", value)
    for name in ("actor", "legal_actions", "external_queue", "reward_ledger",
                 "event_cursor", "policy_state", "terminal"):
        add(name, getattr(state, name))
    if isinstance(state, SearchState):
        add("native_snapshot", state.native_snapshot)
    else:
        add("execution_digest", state.execution_digest)
    return result


def edge_replay_audit_values(state: SearchState | SearchView) -> dict[str, Any]:
    fair = state.observation.get("fair", {})
    causal_handles = {
        owner: [entity["causal_group"]["handle"]
                for entity in observation.get("entities", [])
                if isinstance(entity.get("causal_group"), Mapping)
                and "handle" in entity["causal_group"]]
        for owner, observation in fair.items()
    }
    return {
        "actor": state.actor,
        "terminal": state.terminal,
        "tick": state.observation.get("tick"),
        "native_generation": state.observation.get("native", {}).get("generation"),
        "execution_digest": (state.execution_digest
                             if isinstance(state, SearchView) else None),
        "fair_episode_ids": {owner: observation.get("episode_id")
                              for owner, observation in fair.items()},
        "fair_causal_handles": causal_handles,
        "event_cursor": state.event_cursor,
    }
