from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch
from native_runner.contracts import ActionV1

from packed_action import PackedPrior, packed_space_from_keys
from tree_search import PuctSearch, SearchNode, SearchState, SearchView, Transition, semantic_state_key_v2, state_digest
from native_adapter import NativeSnapshotSearchAdapter, RestorableSearchCheckpoint
from event_policy_rollout import EventPolicyRollout, RootRolloutCapsule
from puct_vs_policy_eval import _view_actor


def test_terminal_view_actor_falls_back_when_no_owner_is_ready():
    rollout = SimpleNamespace(ready=[], learner_owner=1)
    assert _view_actor(rollout) == 1


def test_view_actor_prefers_the_ready_owner():
    rollout = SimpleNamespace(ready=[0], learner_owner=1)
    assert _view_actor(rollout) == 0


def test_packed_prior_decodes_in_canonical_order_and_preserves_tie_break():
    space = packed_space_from_keys(("wait", "play:0:2:0", "play:0:2:1"))
    prior = PackedPrior(space, (1.0, 1.0, 1.0)).normalized()
    assert space.decode_many(range(len(space))) == ("wait", "play:0:2:0", "play:0:2:1")
    assert prior.to_mapping() == {
        "wait": pytest.approx(1 / 3),
        "play:0:2:0": pytest.approx(1 / 3),
        "play:0:2:1": pytest.approx(1 / 3),
    }


def test_packed_prior_top_k_preserves_stable_boundary_ties():
    keys = tuple(["wait"] + [f"play:0:{index}:0" for index in range(5)])
    space = packed_space_from_keys(keys)
    prior = PackedPrior(space, (1.0, 3.0, 3.0, 2.0, 3.0, 0.0)).normalized(top_k=2)

    assert prior.mass.tolist() == pytest.approx([0.0, 0.5, 0.5, 0.0, 0.0, 0.0])


def test_packed_prior_top_k_all_zero_mass_preserves_earliest_actions():
    space = packed_space_from_keys(("wait", "play:0:0:0", "play:0:1:0", "play:0:2:0"))
    prior = PackedPrior(space, (0.0, 0.0, 0.0, 0.0)).normalized(top_k=2)

    assert prior.mass.tolist() == pytest.approx([0.5, 0.5, 0.0, 0.0])


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is unavailable")
def test_packed_prior_moves_search_mass_to_cpu_without_changing_values():
    space = packed_space_from_keys(("wait", "play:0:2:0", "play:0:2:1"))
    source = PackedPrior(space, torch.tensor([1.0, 2.0, 3.0], device="cuda"))
    normalized = source.normalized()
    assert normalized.mass.device.type == "cpu"
    assert normalized.mass.tolist() == pytest.approx([1 / 6, 2 / 6, 3 / 6])


@pytest.mark.parametrize("ability_actions,ability_delay", [(False, False), (True, False), (True, True)])
def test_direct_packed_decode_matches_canonical_space(monkeypatch, ability_actions, ability_delay):
    monkeypatch.setattr(
        "event_policy_rollout.candidate_ability_mask",
        lambda _: torch.tensor([[False, True, False, True]]),
    )
    rollout = object.__new__(EventPolicyRollout)
    rollout.ready = [0]
    rollout.models = {0: SimpleNamespace(config=SimpleNamespace(
        ability_actions=ability_actions, ability_delay=ability_delay))}
    rollout.frames = {0: None}
    rollout.config = SimpleNamespace(delay_offset_ms=(0, 50, 100))
    rollout.legals = {0: SimpleNamespace(
        legal_rows=(1, 0),
        cell_masks=torch.tensor([[False, True, False, True], [True, False, True, False]]),
        delay_masks=torch.tensor([[True, False, True], [False, True, True]]),
    )}
    space = rollout.packed_action_space(0)
    assert tuple(rollout.decode_packed_action(0, index) for index in range(len(space))) == space.canonical_keys()
    with pytest.raises(IndexError):
        rollout.decode_packed_action(0, len(space))
    with pytest.raises(IndexError):
        rollout.decode_packed_action(0, -1)
    with pytest.raises(ValueError, match="active owner"):
        rollout.decode_packed_action(1, 0)


def test_context_cache_is_scoped_to_frame_and_owner(monkeypatch):
    calls = []
    frame = SimpleNamespace(content="frame", to_model_input=lambda *args, **kwargs: "frame")
    monkeypatch.setattr("event_policy_rollout.fast_semantic_digest", lambda item: item.content)
    model = SimpleNamespace(
        initial_state=lambda *args, **kwargs: None,
        encode=lambda *args, **kwargs: calls.append(1) or SimpleNamespace(value=torch.tensor([1.])),
    )
    rollout = object.__new__(EventPolicyRollout)
    rollout.models = {0: model, 1: model}
    rollout.frames = {0: frame, 1: frame}
    rollout.device = torch.device("cpu")
    rollout.profile_sink = None
    rollout.context_cache_enabled = True
    rollout._decision_context_cache = None
    with torch.inference_mode():
        first = rollout.context(0)
        assert rollout.context(0) is first
        rollout.context(1)
        rollout.context(0)
        rollout.frames[0] = SimpleNamespace(content="new", to_model_input=lambda *args, **kwargs: "new")
        rollout.context(0)
    assert len(calls) == 3


def test_context_content_cache_survives_restore_boundary_and_is_owner_scoped(monkeypatch):
    calls = []
    monkeypatch.setattr("event_policy_rollout.fast_semantic_digest", lambda item: item.content)

    def make_frame(content):
        return SimpleNamespace(content=content, to_model_input=lambda *args, **kwargs: content)

    model = SimpleNamespace(
        initial_state=lambda *args, **kwargs: None,
        encode=lambda *args, **kwargs: calls.append(1) or SimpleNamespace(value=torch.tensor([1.])),
    )
    rollout = object.__new__(EventPolicyRollout)
    rollout.models = {0: model, 1: model}
    rollout.frames = {0: make_frame("same"), 1: make_frame("same")}
    rollout.device = torch.device("cpu")
    rollout.profile_sink = lambda *_: None
    rollout.profile_counters = {}
    rollout.context_cache_enabled = True
    rollout.context_content_cache_size = 8
    rollout._decision_context_cache = None
    rollout._decision_context_content_cache = None

    with torch.inference_mode():
        first = rollout.context(0)
        rollout._decision_context_cache = None
        rollout.frames[0] = make_frame("same")
        assert rollout.context(0) is first
        rollout.frames[0] = make_frame("changed")
        rollout.context(0)
        rollout.frames[1] = make_frame("changed")
        rollout.context(1)

    assert len(calls) == 3
    assert rollout.profile_counters["model_context_content_cache_hits"] == 1
    assert rollout.profile_counters["model_context_digest_calls"] == 4
def test_native_step_reuses_current_observations_until_restore(monkeypatch):
    monkeypatch.setattr("event_policy_rollout.policy_actions_open", lambda *_: True)
    observations = [{owner: SimpleNamespace(tick=tick) for owner in (0, 1)}
                    for tick in range(5)]
    class Environment:
        raw_observation = {"tick": 0}
        def __init__(self):
            self.observed = 0
            self.stepped = 0
        def observe_dual(self, **_kwargs):
            self.observed += 1
            return observations[self.stepped]
        def step(self, _commands, **kwargs):
            assert kwargs["timing_profile"] is None
            self.stepped += 1
            self.raw_observation = {"tick": self.stepped}
            return observations[self.stepped], {0: 0., 1: 0.}, {"__all__": False}, {"__all__": False}, {}
        def action_outcomes(self):
            return ()
        def deployment_timing_events(self, _owner):
            return ()
    environment = Environment()
    rollout = object.__new__(EventPolicyRollout)
    rollout.environment = environment
    rollout._step_observations = observations[0]
    rollout._decision_context_cache = None
    rollout.step_timing_profile = None
    rollout.profile_sink = None
    rollout.tick = 0
    rollout.steps = rollout.native_events = 0
    rollout.terminal = False
    rollout.ready = []
    rollout.overflow = None
    rollout._search_execution_ledger = None
    rollout.ledger = SimpleNamespace(reconcile=lambda _: (), ability_events=())
    rollout.recorder = SimpleNamespace(reconcile=lambda _: None, advance=lambda **_: None)
    rollout.recovery_owners = set()
    rollout.totals = {0: 0., 1: 0.}
    rollout.seen_events = set()
    rollout.last_decision_by_owner = {0: 0, 1: 0}
    rollout.config = SimpleNamespace(max_wait_ticks=100)
    rollout.learner_owner = 0
    rollout._due_commands = lambda _: {
        owner: (ActionV1.wait(owner, ticks=1),) for owner in (0, 1)
    }
    rollout.step_native()
    rollout.step_native()
    assert environment.observed == 0
    rollout._step_observations = None
    rollout.step_native()
    assert environment.observed == 1
    rollout.reuse_step_observations = False
    rollout.step_native()
    assert environment.observed == 2


def test_current_policy_observations_reuses_step_result():
    observations = {0: SimpleNamespace(tick=7), 1: SimpleNamespace(tick=7)}
    rollout = object.__new__(EventPolicyRollout)
    rollout._step_observations = observations
    rollout.environment = SimpleNamespace(
        observe_dual=lambda **_: pytest.fail("latest step observations should be reused"))
    assert rollout.current_policy_observations() is observations


def test_epoch_rebind_uses_verified_receipt_without_native_observe():
    rollout = object.__new__(EventPolicyRollout)
    rollout.seen_events = {"5:7", "combat:5:8"}
    rollout.environment = SimpleNamespace(native=SimpleNamespace(
        observe=lambda: pytest.fail("verified epoch must not request native observe")))
    rollout._rebind_seen_event_epoch(9)
    assert rollout.seen_events == {"9:7", "combat:9:8"}


def test_fingerprint_requests_native_digest_once():
    state = SearchState(
        native_snapshot={"handle": 1, "state_sha256": "a" * 64},
        observation={"tick": 1, "truncated": False}, actor=0,
        legal_actions=("wait",), external_queue=(), reward_ledger={},
        event_cursor={}, policy_state={},
    )
    adapter = object.__new__(NativeSnapshotSearchAdapter)
    adapter._active = state
    adapter.validate_state = False
    requests = []
    adapter._native_digest = lambda: requests.append(1) or "a" * 64
    assert adapter.fingerprint() == adapter.fingerprint()
    assert len(requests) == 2


def test_root_view_reuses_active_checkpoint_without_native_capture():
    state = SearchState(
        native_snapshot={"handle": 7, "state_sha256": "a" * 64},
        observation={"tick": 1, "truncated": False}, actor=0,
        legal_actions=("wait",), external_queue=(), reward_ledger={},
        event_cursor={}, policy_state={},
    )
    adapter = object.__new__(NativeSnapshotSearchAdapter)
    adapter._owned = {7}
    adapter._active = state
    adapter.capture_view = lambda: pytest.fail("root view should not recapture")
    checkpoint = RestorableSearchCheckpoint(state)
    view = adapter.view_from_checkpoint(checkpoint)
    assert view.execution_digest == "a" * 64
    assert view.observation is state.observation
    adapter._active = None
    with pytest.raises(ValueError, match="active search state"):
        adapter.view_from_checkpoint(checkpoint)


def test_state_key_copy_on_write_preserves_digest_and_input():
    observation = {
        "tick": 1, "truncated": False, "native": {"generation": 7},
        "fair": {
            "0": {"episode_id": "ep", "entities": [
                {"entity_id": 2, "causal_group": {"handle": "a"}},
                {"entity_id": 1, "causal_group": {"handle": "b"}},
            ]},
            "1": {"episode_id": "ep", "entities": [
                {"entity_id": 3, "causal_group": {"handle": "a"}},
            ]},
        },
    }
    view = SearchView(
        observation=observation, actor=0, legal_actions=("wait",),
        external_queue=(), reward_ledger={}, event_cursor={}, policy_state={},
    )
    assert state_digest(view) == "9dea44c516a4de47dbc7ea30d67c3411a8ae6c8c884edc3ffcd532ee581178a8"
    assert semantic_state_key_v2(view) == "f5f4b5730bf6d23e8cbe4b612415a0b50525b46d64325f549e1644b367f37fba"
    assert observation["native"]["generation"] == 7
    assert observation["fair"]["0"]["entities"][0]["causal_group"]["handle"] == "a"


def test_packed_policy_decodes_only_visited_actions(monkeypatch):
    space = packed_space_from_keys(("wait", "play:0:2:0", "play:0:2:1"))
    state = SearchView(
        observation={"tick": 0, "truncated": False}, actor=0,
        legal_actions=(), external_queue=(), reward_ledger={}, event_cursor={},
        policy_state={}, action_space=space,
    )
    node = SearchNode(state, PackedPrior(space, (1.0, 1.0, 1.0)))
    node.visits[1] = 2
    monkeypatch.setattr(type(space), "canonical_keys", lambda _: pytest.fail("full decode"))
    assert node.policy() == {"play:0:2:0": 1.0}


class PackedFixtureAdapter:
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
        keys = ("left", "right") if not terminal else ()
        space = packed_space_from_keys(("wait", "play:0:0:0")) if not terminal else None
        return SearchView(
            observation={"tick": 0, "truncated": False}, actor=0,
            legal_actions=() if space is not None else keys,
            external_queue=(), reward_ledger={}, event_cursor={}, policy_state={},
            terminal=terminal, execution_digest="a" * 64, action_space=space,
        )

    def apply_action(self, action):
        assert isinstance(action, int)
        self.pending = action

    def advance_current_until_decision(self, _points, _seconds):
        self.current = "child"
        self.pending = None
        return Transition(
            SearchView(observation={"tick": 1, "truncated": False}, actor=0,
                       legal_actions=(), external_queue=(), reward_ledger={},
                       event_cursor={}, policy_state={}, terminal=True,
                       execution_digest="b" * 64),
            1.0, 1, 0.1,
        )

    def fingerprint(self):
        return self.current


def test_puct_can_run_with_numeric_action_tokens():
    adapter = PackedFixtureAdapter()
    space = packed_space_from_keys(("wait", "play:0:0:0"))
    root = SearchView(
        observation={"tick": 0, "truncated": False}, actor=0,
        legal_actions=(), external_queue=(), reward_ledger={}, event_cursor={},
        policy_state={}, terminal=False, execution_digest="a" * 64, action_space=space,
    )
    search = PuctSearch(
        adapter, lambda _: PackedPrior(space, (1.0, 1.0)),
        lambda _: PackedPrior(space, (1.0, 1.0)), lambda _: 0.0,
    )
    result = search.run(4, root_state=root, root_checkpoint="root")
    assert sum(result.visits.values()) == 4
    assert set(result.visits) <= {0, 1}
    assert result.action_space.decode_one(max(result.visits, key=result.visits.get)) in {
        "wait", "play:0:0:0"
    }


def test_root_capsule_digest_rejects_mutation():
    capsule = RootRolloutCapsule(
        "root-rollout-capsule.v1", {"tick": 1}, {}, None, None, None, "wrong"
    )
    with pytest.raises(ValueError, match="mutated"):
        capsule.audit()
