from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_fair_final_search import runtime_world
from native_runner.contracts import TerminalV1
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.policy import FullAction, JointPolicy
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import RolloutSearchWorld


def pending_world(rollout, owner, opponent_action):
    opening = runtime_world(rollout, owner=owner)
    opening.close()
    environment = rollout.runtime.environment
    environment.rejected = {owner}
    rollout.commit_simultaneous({owner: FullAction(11, 0, 1), 1-owner: opponent_action})
    rollout.advance()
    assert rollout.advance() == {owner: "rejected"}
    environment.rejected.clear()
    information = rollout.freeze_due_information()
    assert set(information) == {owner}
    world = RolloutSearchWorld(rollout, owner=owner, sidecar_factory=None,
        manifest=dict(source="independent-belief", actual_state_copied=False,
            root_fair_sha=information[owner].sha256))
    return world, information[owner]


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("opponent_action", [FullAction(), FullAction(11, 0, 30)])
@pytest.mark.parametrize("own_action", [FullAction(), FullAction(11, 0, 1), FullAction(11, 0, 29)])
def test_pending_plan_settlement_and_sampling_replay_identically(rollout, owner, opponent_action, own_action):
    world, information = pending_world(rollout, owner, opponent_action)
    original_clock = rollout.runtime.clock.capture()
    original_rng = {seat: generator.get_state().clone() for seat, generator in rollout.generators.items()}
    branches = []
    sampled = []

    def sample(current, generator):
        assert current.owner == 1-owner and current.tick == 160
        assert rollout.runtime.clock.pending[owner].due_tick == 162
        sampled.append(current.tick)
        return FullAction()

    try:
        for attempt in range(2):
            world.restore_root()
            assert rollout.runtime.clock.capture() == original_clock
            assert world.root_information().sha256 == information.sha256
            sampled.clear()
            result = world.transition(own_action, opponent=SimpleNamespace(sample=sample),
                generator=torch.Generator().manual_seed(42), hard_tick=200,
                maximum_decisions=6, check_deadline=lambda: None)
            assert result.completed and result.terminal_value is None
            assert result.information.owner == owner
            assert rollout.runtime.clock.boundary(owner)
            if own_action.card_id is not None:
                assert result.reason == "executed" and result.ticks == own_action.delay_slot*2
                assert sampled == [] and result.decisions == 1
            elif opponent_action.card_id is None:
                assert result.reason == "wait_expired" and result.ticks == 60
                assert sampled == [160] and result.decisions == 2
                assert rollout.runtime.clock.pending[1-owner].due_tick == 220
            else:
                assert result.reason == "preempted" and result.ticks == 58
                assert sampled == [] and result.decisions == 1
            if own_action.delay_slot == 29:
                settled = {entry["owner"]: entry["reason"]
                    for entry in rollout.runtime.clock.evidence if entry["tick"] == 160}
                assert settled == {owner: "executed",
                    1-owner: "wait_expired" if opponent_action.card_id is None else "executed"}
            branches.append(dict(clock=rollout.runtime.clock.capture(), fair_sha=result.information.sha256,
                executions=deepcopy(rollout.runtime.environment.executions)))
    finally:
        world.close()
    assert branches[0] == branches[1]
    assert rollout.runtime.clock.capture() == original_clock
    assert rollout.freeze_due_information()[owner].sha256 == information.sha256
    for seat, generator in rollout.generators.items():
        assert torch.equal(generator.get_state(), original_rng[seat])
    assert world.root.native.closed


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("hard_tick,maximum_decisions,ticks,decisions,samples", [
    (159, 6, 57, 1, 0), (160, 6, 58, 2, 1), (200, 1, 58, 1, 0)])
def test_pending_edge_at_limits_restores_root_without_leaf(rollout, owner, hard_tick,
        maximum_decisions, ticks, decisions, samples):
    world, information = pending_world(rollout, owner, FullAction())
    original_clock = rollout.runtime.clock.capture()

    sampled = []

    def sample(current, generator):
        assert current.owner == 1-owner and current.tick == 160
        assert rollout.runtime.clock.boundary(1-owner)
        sampled.append(current.tick)
        return FullAction()

    try:
        result = world.transition(FullAction(), opponent=SimpleNamespace(sample=sample),
            generator=torch.Generator(), hard_tick=hard_tick, maximum_decisions=maximum_decisions,
            check_deadline=lambda: None)
        assert not result.completed and result.information is None and result.terminal_value is None
        assert (result.ticks, result.decisions) == (ticks, decisions)
        assert len(sampled) == samples
        assert rollout.runtime.clock.capture() == original_clock
        assert world.root_information().sha256 == information.sha256
    finally:
        world.close()


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("winner", [0, 1, None])
def test_pending_terminal_uses_native_result_and_root_player_perspective(rollout, owner, winner):
    world, information = pending_world(rollout, owner, FullAction())
    original_clock = rollout.runtime.clock.capture()
    environment = rollout.runtime.environment
    environment.terminal_tick = 159
    scores = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    terminal = TerminalV1(ended=True, winner=winner, result_by_owner=scores, terminal_tick=159)
    environment.raw_observation = dict(finalized=True, winner=winner,
        worldResult=-1 if winner is None else winner, worldResultRaw=-1 if winner is None else winner,
        crownsRaw=[0, 0] if winner is None else [1, 0])
    original_observe = environment.observe_dual

    def observe(**kwargs):
        return {seat: replace(observation, terminal=terminal)
            for seat, observation in original_observe(**kwargs).items()}

    def unexpected_sample(*args):
        raise AssertionError("terminal precedes the pending opponent wake")

    environment.observe_dual = observe
    try:
        result = world.transition(FullAction(), opponent=SimpleNamespace(sample=unexpected_sample),
            generator=torch.Generator(), hard_tick=200, maximum_decisions=6, check_deadline=lambda: None)
        assert result.completed and result.information is None
        assert result.terminal_value == scores[owner] and result.reason == "terminal_expired"
        assert result.ticks == 57 and result.decisions == 1
        assert rollout.runtime.clock.ready == set() and rollout.runtime.clock.pending == {}
    finally:
        world.close()
    assert rollout.runtime.clock.capture() == original_clock
    assert rollout.freeze_due_information()[owner].sha256 == information.sha256


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("opponent_action", [FullAction(), FullAction(11, 0, 30)])
@pytest.mark.parametrize("mode,budget", [("FULL", 128), ("FAST", 16)])
def test_pending_root_search_counts_only_completed_edges_and_restores(rollout, owner, opponent_action,
        mode, budget):
    world, information = pending_world(rollout, owner, opponent_action)
    original_clock = rollout.runtime.clock.capture()
    original_rng = {seat: generator.get_state().clone() for seat, generator in rollout.generators.items()}

    class Inference:
        checkpoint_sha = "c"*64
        controller_sha = "b"*64

        def __init__(self):
            self.seen = []

        def predict(self, current):
            assert current.owner == owner and rollout.runtime.clock.boundary(owner)
            assert current.owner not in rollout.runtime.clock.pending
            self.seen.append(current.tick)
            policy = JointPolicy(lambda prefix: torch.zeros_like(current.legality.mask(prefix),
                dtype=torch.float32), current.legality)
            return policy, torch.tensor([[1., 0., -1.]])

        def sample(self, current, generator):
            assert current.owner == 1-owner and rollout.runtime.clock.boundary(1-owner)
            assert current.tick >= 160
            return FullAction()

    class Belief:
        sampler_sha = "d"*64

        def __init__(self):
            self.builds = 0

        def build(self, current, *, seed):
            assert current.sha256 == information.sha256
            self.builds += 1
            return world

    inference, belief = Inference(), Belief()
    limits = SearchLimits(soft_decisions=1, soft_ticks=60)
    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=belief.sampler_sha,
        checkpoint_blue_sha=inference.checkpoint_sha, checkpoint_red_sha=inference.checkpoint_sha,
        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
    search = CompletedBoundarySearch(belief, inference, inference, limits=limits)
    outcome = search.run(information, seed=17, behavior_seed=18, identity=identity, mode=mode,
        codec=ActionCodec([11, 22, 33, 44, 55, 66, 77, 88]))
    assert belief.builds == 1 and world.closed and world.root.native.closed
    assert outcome.result["valid_simulations"] == budget
    assert outcome.result["invalid_attempts"] == 0
    assert outcome.result["unfinished_edge_backups"] == 0
    assert sum(entry["visits"] for entry in outcome.result["candidates"]) == budget
    assert sum(outcome.audit["completed_lifecycle"].values()) == budget
    assert len(inference.seen) > 1
    assert (outcome.policy_target is None) == (mode == "FAST")
    if mode == "FULL":
        assert sum(count for action, count in outcome.policy_target) == 128
    assert information.legality.legal(outcome.selected)
    assert rollout.runtime.clock.capture() == original_clock
    assert rollout.freeze_due_information()[owner].sha256 == information.sha256
    for seat, generator in rollout.generators.items():
        assert torch.equal(generator.get_state(), original_rng[seat])
