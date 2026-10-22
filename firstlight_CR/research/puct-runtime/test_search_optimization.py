from dataclasses import replace

import pytest
import torch

from tree_search import PuctSearch, SearchState, SearchView, Transition, state_digest


class OptimizedFixtureAdapter:
    def __init__(self):
        self.current = "root"
        self.pending = None

    def checkpoint_restorable(self):
        return "root"

    def restore(self, checkpoint):
        self.current = checkpoint
        self.pending = None

    def capture_current_view(self):
        terminal = self.current != "root"
        actions = () if terminal else ("left", "right")
        return SearchView(
            observation={"tick": 0 if terminal else 10, "truncated": False,
                         "large_history": list(range(100))},
            actor=0, legal_actions=actions, external_queue=(),
            reward_ledger={"history": list(range(100))},
            event_cursor={"cursor": 100},
            policy_state={"sidecar": {"large": list(range(100))}},
            terminal=terminal, execution_digest="f" * 64,
        )

    def apply_action(self, action):
        self.pending = action

    def advance_current_until_decision(self, _points, _seconds):
        self.current = self.pending
        self.pending = None
        return Transition(self.capture_current_view(),
                          1. if self.current == "left" else 0., 1, .1)

    def fingerprint(self):
        return self.current


def test_optimized_children_keep_only_selection_state():
    adapter = OptimizedFixtureAdapter()
    search = PuctSearch(
        adapter, lambda _: {"left": .5, "right": .5}, lambda _: {}, lambda _: 0.,
        compact_children=True,
    )
    root = search.run(8, root_state=adapter.capture_current_view(), root_checkpoint="root")
    assert sum(root.visits.values()) == 8
    assert len(root.children) == 2
    for child in root.children.values():
        assert child.state.observation == {"tick": child.state.observation["tick"]}
        assert child.state.reward_ledger == {}
        assert child.state.event_cursor == {}
        assert child.state.policy_state == {}


def test_state_digest_hashes_tensor_bytes_and_binds_execution_digest():
    base = SearchView(
        observation={"truncated": False, "tensor": torch.tensor([1., 2.])},
        actor=0, legal_actions=("wait",), external_queue=(), reward_ledger={},
        event_cursor={}, policy_state={}, execution_digest="a" * 64,
    )
    original = base.observation["tensor"].clone()
    first = state_digest(base)
    assert torch.equal(base.observation["tensor"], original)
    changed_tensor = replace(base, observation={"truncated": False,
                                                "tensor": torch.tensor([1., 3.])})
    changed_execution = replace(base, execution_digest="b" * 64)
    assert state_digest(changed_tensor) != first
    assert state_digest(changed_execution) != first


def test_zero_tick_actor_switch_has_distinct_search_key():
    first = SearchView(
        observation={"tick": 200, "truncated": False}, actor=0,
        legal_actions=("wait",), external_queue=(), reward_ledger={},
        event_cursor={"seen": [], "combat": 0, "public_card_play": None},
        policy_state={}, execution_digest="a" * 64,
    )
    switched = replace(first, actor=1)
    assert state_digest(first) != state_digest(switched)


def test_model_profile_span_closes_on_exception():
    class ProfiledAdapter(OptimizedFixtureAdapter):
        profile_enabled = True

        def __init__(self):
            super().__init__()
            self.spans = []

        def _add_profile(self, name, started):
            self.spans.append(name)

    adapter = ProfiledAdapter()

    def broken_prior(_state):
        raise RuntimeError("fixture prior failure")

    search = PuctSearch(adapter, broken_prior, lambda _: {}, lambda _: 0.)
    with pytest.raises(RuntimeError, match="fixture prior failure"):
        search.run(1, root_state=adapter.capture_current_view(), root_checkpoint="root")
    assert adapter.spans == ["policy_prior_ms"]


class LegacyFixtureAdapter:
    def __init__(self):
        self.current = "root"
        self.pending = None

    def save_search_state(self):
        terminal = self.current != "root"
        return SearchState(
            native_snapshot={"state": self.current},
            observation={"tick": 0 if terminal else 10, "truncated": False},
            actor=0, legal_actions=() if terminal else ("left", "right"),
            external_queue=(), reward_ledger={"state": self.current},
            event_cursor={"state": self.current}, policy_state={"state": self.current},
            terminal=terminal,
        )

    def restore_search_state(self, state):
        self.current = state.native_snapshot["state"]
        self.pending = None

    def apply_action(self, action):
        self.pending = action

    def advance_until_decision(self, _points, _seconds):
        self.current = self.pending
        self.pending = None
        return Transition(self.save_search_state(), 1. if self.current == "left" else 0., 1, .1)

    def fingerprint(self):
        return self.current

    def release_search_state(self, _state):
        pass


def test_legacy_and_optimized_visit_q_and_selection_match():
    legacy = LegacyFixtureAdapter()
    optimized = OptimizedFixtureAdapter()
    legacy_search = PuctSearch(
        legacy, lambda _: {"left": .5, "right": .5}, lambda _: {}, lambda _: 0.,
        compact_children=True,
    )
    optimized_search = PuctSearch(
        optimized, lambda _: {"left": .5, "right": .5}, lambda _: {}, lambda _: 0.,
        compact_children=True,
    )
    legacy_root = legacy_search.run(32)
    optimized_root = optimized_search.run(
        32, root_state=optimized.capture_current_view(), root_checkpoint="root",
    )
    assert legacy_root.visits == optimized_root.visits
    assert legacy_root.value_sum == optimized_root.value_sum
    assert legacy_root.policy(1.) == optimized_root.policy(1.)
    assert max(legacy_root.prior, key=lambda a: (legacy_root.visits[a], a)) == max(
        optimized_root.prior, key=lambda a: (optimized_root.visits[a], a)
    )
