import pytest

from tree_search import (
    DeterministicEdgeMismatch,
    PuctSearch,
    SearchView,
    Transition,
    semantic_state_key_v2,
    state_digest,
)


class EdgeMemoAdapter:
    profile_enabled = True

    def __init__(self, *, mismatch=None):
        self.current = "root"
        self.pending = None
        self.capture_count = 0
        self.viewless_count = 0
        self.transition_count = 0
        self.mismatch = mismatch
        self.counters = {}

    def checkpoint_restorable(self):
        return "root"

    def restore(self, checkpoint):
        self.current = checkpoint
        self.pending = None

    def capture_current_view(self):
        self.capture_count += 1
        terminal = self.current != "root"
        drifted = self.mismatch == "state" and self.transition_count > 2
        return SearchView(
            observation={"tick": 0 if terminal else 10, "truncated": False,
                         "drifted": drifted},
            actor=0,
            legal_actions=() if terminal else ("left", "right"),
            external_queue=(), reward_ledger={}, event_cursor={}, policy_state={},
            terminal=terminal,
        )

    def apply_action(self, action):
        self.pending = action

    def advance_current_until_decision(self, _points, _seconds):
        self.transition_count += 1
        self.current = self.pending
        self.pending = None
        reward = 1.0 if self.current == "left" else 0.0
        if self.mismatch == "reward" and self.transition_count > 2:
            reward += 1.0
        return Transition(self.capture_current_view(), reward, 1, 0.1)

    def advance_current_without_view(self, _points, _seconds):
        self.viewless_count += 1
        self.transition_count += 1
        self.current = self.pending
        self.pending = None
        reward = 1.0 if self.current == "left" else 0.0
        if self.mismatch == "reward" and self.transition_count > 2:
            reward += 1.0
        return Transition(None, reward, 1, 0.1)

    def fingerprint(self):
        return self.current

    def _count_profile(self, name, amount=1):
        self.counters[name] = self.counters.get(name, 0) + amount


def run_search(adapter, *, edge_memo, verify_every):
    root_state = adapter.capture_current_view()
    search = PuctSearch(
        adapter,
        lambda _: {"left": 0.5, "right": 0.5},
        lambda _: {},
        lambda _: 0.0,
        compact_children=True,
        edge_memo=edge_memo,
        edge_verify_every=verify_every,
    )
    root = search.run(32, root_state=root_state, root_checkpoint="root")
    return root


def test_edge_memo_verified_replays_preserve_search_semantics():
    baseline_adapter = EdgeMemoAdapter()
    memo_adapter = EdgeMemoAdapter()
    baseline = run_search(baseline_adapter, edge_memo=False, verify_every=1)
    memo = run_search(memo_adapter, edge_memo=True, verify_every=1)

    assert memo.visits == baseline.visits
    assert memo.value_sum == baseline.value_sum
    assert memo.policy(1.0) == baseline.policy(1.0)
    assert memo_adapter.counters["edge_memo_verifications"] > 0
    assert memo_adapter.viewless_count == 0


def test_edge_memo_viewless_replay_reduces_child_view_capture():
    baseline_adapter = EdgeMemoAdapter()
    memo_adapter = EdgeMemoAdapter()
    baseline = run_search(baseline_adapter, edge_memo=False, verify_every=1)
    memo = run_search(memo_adapter, edge_memo=True, verify_every=0)

    assert memo.visits == baseline.visits
    assert memo.value_sum == baseline.value_sum
    assert memo.policy(1.0) == baseline.policy(1.0)
    assert memo_adapter.viewless_count > 0
    assert memo_adapter.capture_count < baseline_adapter.capture_count


def test_edge_memo_mismatch_fails_closed():
    adapter = EdgeMemoAdapter(mismatch="reward")
    root_state = adapter.capture_current_view()
    search = PuctSearch(
        adapter,
        lambda _: {"left": 0.5, "right": 0.5},
        lambda _: {},
        lambda _: 0.0,
        edge_memo=True,
        edge_verify_every=1,
    )

    with pytest.raises(DeterministicEdgeMismatch, match="changed reward"):
        search.run(32, root_state=root_state, root_checkpoint="root")


def test_edge_memo_semantic_key_mismatch_fails_closed():
    adapter = EdgeMemoAdapter(mismatch="state")
    root_state = adapter.capture_current_view()
    search = PuctSearch(
        adapter,
        lambda _: {"left": 0.5, "right": 0.5},
        lambda _: {},
        lambda _: 0.0,
        edge_memo=True,
        edge_verify_every=1,
        edge_component_diagnostics=True,
    )

    with pytest.raises(DeterministicEdgeMismatch, match="semantic child state") as error:
        search.run(32, root_state=root_state, root_checkpoint="root")
    assert "observation.drifted" in str(error.value)


def test_semantic_key_v2_ignores_operational_cursor_and_episode_offsets():
    def view(epoch, sequence, handle, episode_id, tick=393):
        return SearchView(
            observation={
                "tick": tick,
                "truncated": False,
                "fair": {"0": {
                    "episode_id": episode_id,
                    "entities": [{"entity_id": 1,
                                  "causal_group": {"handle": handle}}],
                }},
            },
            actor=0, legal_actions=("wait",), external_queue=(),
            reward_ledger={},
            event_cursor={
                "seen": [f"{epoch}:{sequence - 3}"],
                "combat": sequence,
                "public_card_play": None,
                "native": {"combat": ([epoch, epoch * 10], sequence)},
            },
            policy_state={}, execution_digest="f" * 64,
        )

    first = view(1, 10, "handle-a", "episode-a")
    shifted = view(2, 14, "handle-b", "episode-b")
    changed_game_state = view(2, 14, "handle-b", "episode-b", tick=394)

    assert state_digest(first) != state_digest(shifted)
    assert semantic_state_key_v2(first) == semantic_state_key_v2(shifted)
    assert semantic_state_key_v2(first) != semantic_state_key_v2(changed_game_state)
