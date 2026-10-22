from dataclasses import fields, replace
from types import SimpleNamespace
import time

import pytest
import torch

from native_runner.tests.test_fair_final_protocol import toy_policy
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_training_tracking import _observation
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.policy import FullAction, JointPolicy
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits, SearchNode, Transition
from native_runner.training.fair_final.search_runtime import RolloutSearchWorld


class Inference:
    checkpoint_sha = "a" * 64
    controller_sha = "b" * 64

    def __init__(self):
        self.frames = []

    def predict(self, info):
        self.frames.append((info.owner, info.tick))
        return toy_policy()[0], torch.tensor([[.4, -.2, -.7]])


class World:
    commitment = "c" * 64
    manifest = {"source": "unit-test-independent-world"}

    def __init__(self, info, *, fail_first=0, incomplete_after=None, terminal=None):
        self.root = self.current = info
        self.closed = False
        self.calls = 0
        self.depth = 0
        self.fail_first = fail_first
        self.incomplete_after = incomplete_after
        self.terminal = terminal

    def root_information(self):
        return self.current

    root_owner_information = root_information

    def restore_root(self):
        self.current = self.root
        self.depth = 0

    def transition(self, action, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_first or self.incomplete_after == self.depth:
            return Transition(False, 1, 1)
        self.depth += 1
        if self.terminal is not None:
            return Transition(True, 1, 2, terminal_value=self.terminal, reason="executed")
        self.current = replace(self.current, tick=self.current.tick+2)
        return Transition(True, 1, 2, information=self.current, reason="preempted")

    def close(self):
        self.closed = True


class Belief:
    sampler_sha = "d" * 64

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.builds = 0

    def build(self, info, *, seed):
        self.builds += 1
        self.world = World(info, **self.kwargs)
        return self.world


def search_fixture(*, owner=0, limits=None, **kwargs):
    policy, _ = toy_policy()
    info = DecisionInformation(owner, 100, {"visible": True}, policy.legality)
    inference = Inference()
    belief = Belief(**kwargs)
    limits = limits or SearchLimits()
    search = CompletedBoundarySearch(belief, inference, inference, limits=limits)
    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=belief.sampler_sha,
        checkpoint_blue_sha=inference.checkpoint_sha, checkpoint_red_sha=inference.checkpoint_sha,
        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
    arguments = dict(seed=42, behavior_seed=17, identity=identity,
        codec=ActionCodec([*policy.legality.card_ids, 11, 22, 33, 44, 55, 66]))
    return search, info, arguments


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("mode,budget", [("FULL", 128), ("FAST", 16)])
def test_exact_effective_budget_one_world_and_separate_policy_targets(owner, mode, budget):
    search, info, arguments = search_fixture(owner=owner)
    outcome = search.run(info, mode=mode, **arguments)
    assert outcome.result["valid_simulations"] == budget
    assert sum(entry["visits"] for entry in outcome.result["candidates"]) == budget
    assert outcome.result["unfinished_edge_backups"] == 0
    assert search.belief.builds == 1 and search.belief.world.closed
    assert {owner for owner, tick in search.inference.frames} == {info.owner}
    assert info.legality.legal(outcome.selected)
    assert (outcome.policy_target is None) == (mode == "FAST")
    if mode == "FULL":
        assert sum(count for action, count in outcome.policy_target) == 128


def test_incomplete_root_attempts_never_get_fake_visits():
    search, info, arguments = search_fixture(fail_first=3)
    outcome = search.run(info, **arguments)
    assert outcome.result["invalid_attempts"] == 3
    assert outcome.audit["attempts"] == 131
    assert outcome.audit["discarded_edges"] == 3
    assert sum(count for action, count in outcome.policy_target) == 128


def test_persistent_unfinished_root_is_bounded_and_closes_world():
    search, info, arguments = search_fixture(incomplete_after=0, limits=SearchLimits(maximum_invalid_attempts=3))
    with pytest.raises(ValueError, match="quarantined"):
        search.run(info, **arguments)
    assert search.belief.world.calls == 3 and search.belief.world.closed


def test_incomplete_descendant_backs_up_only_completed_prefix():
    search, info, arguments = search_fixture(incomplete_after=1)
    outcome = search.run(info, **arguments)
    assert outcome.audit["discarded_edges"] > 0
    assert outcome.result["invalid_attempts"] == 0
    assert sum(outcome.audit["completed_lifecycle"].values()) == 128
    assert len(search.inference.frames) == outcome.audit["nodes"]


@pytest.mark.parametrize("value", [-1., 0., 1.])
@pytest.mark.parametrize("owner", [0, 1])
def test_terminal_value_is_already_root_relative_without_event_sign_flip(owner, value):
    search, info, arguments = search_fixture(owner=owner, terminal=value)
    outcome = search.run(info, **arguments)
    assert all(entry["q"] == value for entry in outcome.result["candidates"] if entry["visits"])


def test_behavior_rng_is_separate_from_world_tree_and_candidates():
    first, info, arguments = search_fixture()
    second, _, _ = search_fixture()
    left = first.run(info, **arguments)
    right = second.run(info, **{**arguments, "behavior_seed": 999})
    assert left.result == right.result and left.policy_target == right.policy_target


def test_progressive_opening_keeps_priors_and_card_coverage():
    policy, _ = toy_policy()
    node = SearchNode(policy, torch.zeros(3), limits=SearchLimits(), seed=42)
    before = dict(node.priors)
    for count in range(128):
        action = node.select(SearchLimits())
        node.visits[action.key] += 1
    assert node.priors == before
    assert {entry.action.card_id for entry in node.order[:node.minimum_width]} == {None, *policy.legality.card_ids}


def test_act_only_root_noise_keeps_wait_mass():
    search, info, arguments = search_fixture(limits=SearchLimits(root_noise=.25, root_noise_mode="training"))
    outcome = search.run(info, **arguments)
    wait = next(entry for entry in outcome.result["candidates"] if entry["action_id"] == 0)
    assert wait["prior_projected"] == wait["prior_search"]
    assert sum(entry["prior_search"] for entry in outcome.result["candidates"]) == pytest.approx(1.)


def test_deadline_never_returns_partial_training_labels():
    search, info, arguments = search_fixture()
    with pytest.raises(TimeoutError):
        search.run(info, deadline=time.monotonic()-1, **arguments)
    assert search.belief.builds == 0


@pytest.mark.parametrize("field,value", [("maximum_candidates", 33), ("soft_ticks", 0),
    ("soft_decisions", True), ("root_noise", 1.1), ("cpuct", float("nan"))])
def test_invalid_search_limits_rejected(field, value):
    with pytest.raises(ValueError):
        SearchLimits(**{field: value})


def runtime_world(rollout, owner=0):
    original_observe = rollout.runtime.environment.observe_dual

    def observe(**kwargs):
        observations = original_observe(**kwargs)
        return {seat: replace(_observation(tick=observation.tick), owner=seat)
            if seat == 0 else _opponent_observation(observation.tick) for seat, observation in observations.items()}

    rollout.runtime.environment.observe_dual = observe
    rollout.runtime.frozen = observe()
    info = rollout.freeze_due_information()[owner]
    return RolloutSearchWorld(rollout, owner=owner, sidecar_factory=None,
        manifest=dict(source="independent-belief", actual_state_copied=False, root_fair_sha=info.sha256))


def _opponent_observation(tick):
    observation = _observation(tick=tick)
    return replace(observation, owner=1, players=tuple(replace(player, owner=1-player.owner) for player in observation.players))


class WaitingOpponent:
    def __init__(self, rollout):
        self.rollout, self.seen = rollout, []

    def sample(self, info, generator):
        assert not self.rollout.runtime.clock.pending
        assert info.owner == 1
        self.seen.append(info.sha256)
        return FullAction()


def test_runtime_same_tick_sampling_precedes_any_commit_and_completed_edge(rollout):
    with_world = runtime_world(rollout)
    opponent = WaitingOpponent(rollout)
    try:
        transition = with_world.transition(FullAction(11, 0, 1), opponent=opponent,
            generator=torch.Generator(), hard_tick=200, maximum_decisions=6, check_deadline=lambda: None)
        assert transition.completed and transition.ticks == 2 and transition.reason == "executed"
        assert transition.information.owner == 0
        assert len(opponent.seen) == 1
    finally:
        with_world.close()
    assert rollout.runtime.clock.tick == 100 and not rollout.runtime.clock.pending


def test_runtime_hard_limit_restores_boundary_and_pending_edge_gets_no_value(rollout):
    world = runtime_world(rollout)
    before = world.root_information().sha256
    try:
        transition = world.transition(FullAction(), opponent=WaitingOpponent(rollout),
            generator=torch.Generator(), hard_tick=101, maximum_decisions=6, check_deadline=lambda: None)
        assert not transition.completed and transition.information is None and transition.terminal_value is None
        assert world.root_owner_information().sha256 == before
        assert rollout.runtime.clock.evidence == [] and rollout.runtime.clock.pending == {}
    finally:
        world.close()


@pytest.mark.parametrize("reason", ["preempted", "rejected", "wait_expired"])
def test_runtime_completed_lifecycle_counts_even_when_no_card_is_deployed(rollout, reason):
    world = runtime_world(rollout)
    action = FullAction(11, 0, 1)
    if reason == "preempted":
        rollout.runtime.environment.events = {101}
    elif reason == "rejected":
        rollout.runtime.environment.rejected = {0}
    else:
        action = FullAction()
    try:
        result = world.transition(action, opponent=WaitingOpponent(rollout), generator=torch.Generator(),
            hard_tick=200, maximum_decisions=6, check_deadline=lambda: None)
        assert result.completed and result.reason == reason
    finally:
        world.close()


def test_opponent_gets_identical_frozen_information_for_different_own_choices(rollout):
    world = runtime_world(rollout)
    opponent = WaitingOpponent(rollout)
    try:
        for action in (FullAction(), FullAction(11, 0, 1), FullAction(11, 0, 30)):
            world.restore_root()
            world.transition(action, opponent=opponent, generator=torch.Generator().manual_seed(19),
                hard_tick=101, maximum_decisions=6, check_deadline=lambda: None)
        assert len(opponent.seen) == 3 and len(set(opponent.seen)) == 1
    finally:
        world.close()


def test_opponent_private_fields_are_checked_before_sampling(rollout):
    world = runtime_world(rollout)
    opponent = WaitingOpponent(rollout)
    own_observation = _observation(tick=100)
    poisoned = SimpleNamespace(**{field.name: getattr(own_observation, field.name) for field in fields(own_observation)})
    poisoned.owner = 1
    rollout.runtime.frozen[1] = poisoned
    try:
        with pytest.raises(ValueError, match="opponent private state"):
            world.transition(FullAction(), opponent=opponent, generator=torch.Generator(),
                hard_tick=200, maximum_decisions=6, check_deadline=lambda: None)
        assert opponent.seen == []
    finally:
        world.close()


def test_separate_opponent_events_preserve_own_pending_until_settled(rollout):
    world = runtime_world(rollout)
    rollout.runtime.environment.rejected = {1}
    seen = []

    class RejectedOpponent:
        def sample(self, info, generator):
            assert info.owner == 1
            seen.append(info.tick)
            return FullAction(11, 0, 1)

    try:
        result = world.transition(FullAction(), opponent=RejectedOpponent(), generator=torch.Generator(),
            hard_tick=200, maximum_decisions=2, check_deadline=lambda: None)
        assert seen == [100, 102]
        assert not result.completed and result.decisions == 2 and result.ticks == 4
        assert world.root_owner_information().tick == 100
        assert not rollout.runtime.clock.pending and not rollout.runtime.clock.evidence
    finally:
        world.close()


def test_belief_root_mismatch_closes_world_without_labels():
    search, info, arguments = search_fixture()
    original = search.belief.build

    def build(information, *, seed):
        world = original(information, seed=seed)
        world.root = replace(information, tick=101)
        return world

    search.belief.build = build
    with pytest.raises(ValueError, match="differs from FAIR root"):
        search.run(info, **arguments)
    assert search.belief.world.closed


def test_inference_failure_closes_world_without_partial_result():
    search, info, arguments = search_fixture()

    def fail(info):
        raise RuntimeError("inference fixture failure")

    search.inference.predict = fail
    with pytest.raises(RuntimeError, match="fixture failure"):
        search.run(info, **arguments)
    assert search.belief.world.closed


def test_frozen_inference_rejects_train_mode_and_restored_mode_drift():
    from native_runner.training.fair_final.search_runtime import FrozenSearchInference

    model = torch.nn.Linear(1, 3)
    with pytest.raises(ValueError, match="eval-mode"):
        FrozenSearchInference(model, checkpoint_sha="a"*64, controller_sha="b"*64)
    inference = FrozenSearchInference(model.eval(), checkpoint_sha="a"*64, controller_sha="b"*64)
    model.train()
    with pytest.raises(ValueError, match="training mode"):
        inference.predict(None)


def test_opening_only_belief_rejects_later_roots_before_accessing_an_engine():
    from native_runner.training.fair_final.opening_belief import OpeningEvidence

    _, info, _ = search_fixture()
    with pytest.raises(ValueError, match="opening-only"):
        OpeningEvidence.from_view(_observation(tick=1200), replace(info, tick=1200), opening_tick=100)


@pytest.mark.parametrize("winner", [0, 1, None])
@pytest.mark.parametrize("owner", [0, 1])
def test_runtime_native_terminal_maps_each_root_player_without_pending_value(rollout, owner, winner):
    from native_runner.contracts import TerminalV1

    world = runtime_world(rollout, owner=owner)
    environment = rollout.runtime.environment
    environment.terminal_tick = 101
    scores = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    terminal = TerminalV1(ended=True, winner=winner, result_by_owner=scores, terminal_tick=101)
    environment.raw_observation = dict(finalized=True, winner=winner, worldResult=-1 if winner is None else winner,
        worldResultRaw=-1 if winner is None else winner, crownsRaw=[0, 0] if winner is None else [1, 0])
    original_observe = environment.observe_dual

    def observe(**kwargs):
        return {seat: replace(observation, terminal=terminal) for seat, observation in original_observe(**kwargs).items()}

    environment.observe_dual = observe
    opponent = SimpleNamespace(sample=lambda info, generator: FullAction())
    try:
        result = world.transition(FullAction(), opponent=opponent, generator=torch.Generator(),
            hard_tick=200, maximum_decisions=6, check_deadline=lambda: None)
        assert result.completed and result.information is None and result.reason == "terminal_expired"
        assert result.terminal_value == scores[owner]
    finally:
        world.close()


def test_runtime_truncation_fails_instead_of_becoming_draw(rollout):
    world = runtime_world(rollout)
    original = rollout.runtime.environment.advance_reservations

    def truncate(plans):
        outcomes, result = original(plans)
        return outcomes, (result[0], result[1], result[2], {"__all__": True}, result[4])

    rollout.runtime.environment.advance_reservations = truncate
    try:
        with pytest.raises(ValueError, match="truncated"):
            world.transition(FullAction(), opponent=WaitingOpponent(rollout), generator=torch.Generator(),
                hard_tick=200, maximum_decisions=6, check_deadline=lambda: None)
    finally:
        world.close()

def test_first_visit_prior_vs_legacy_lexical_are_independently_versioned():
    policy, _ = toy_policy()
    prior = SearchLimits(first_visit_rule="highest_prior")
    legacy = SearchLimits(first_visit_rule="legacy_lexical")
    assert prior.sha256 != legacy.sha256
    node = SearchNode(policy, torch.zeros(3), limits=prior, seed=42)
    actions = node.order[:node.minimum_width]
    expected = min(actions, key=lambda entry: (-node.priors[entry.action.key],
        entry.action.key)).action
    assert node.select(prior) == expected
    old = min(actions, key=lambda entry: entry.action.key).action
    assert node.select(legacy) == old


def test_root_noise_modes_fail_closed_and_keep_separate_protocol_hashes():
    clean = SearchLimits(root_noise_mode="evaluation", root_noise=0.)
    training = SearchLimits(root_noise_mode="training", root_noise=.25)
    no_noise_ablation = SearchLimits(root_noise_mode="ablation_no_noise", root_noise=0.)
    assert len({x.sha256 for x in (clean, training, no_noise_ablation)}) == 3
    for mode, amount in (("training", 0.), ("evaluation", .25),
            ("ablation_no_noise", .25), ("unknown", 0.)):
        with pytest.raises(ValueError, match="noise|exploration"):
            SearchLimits(root_noise_mode=mode, root_noise=amount)
    search, info, arguments = search_fixture(limits=clean)
    result = search.run(info, **arguments)
    assert result.result["noise_fraction"] == 0.
    assert all(entry["prior_projected"] == entry["prior_search"]
        for entry in result.result["candidates"])
    train_search, info, arguments = search_fixture(limits=training)
    train_result = train_search.run(info, **arguments)
    assert train_result.result["noise_fraction"] == .25
    assert train_result.result["search_protocol_sha"] != result.result["search_protocol_sha"]

@pytest.mark.parametrize("rule", ["highest_prior", "legacy_lexical"])
def test_same_128_effective_budget_for_first_visit_ablation(rule):
    search, info, arguments = search_fixture(limits=SearchLimits(first_visit_rule=rule))
    result = search.run(info, **arguments)
    assert result.result["valid_simulations"] == 128
    assert sum(entry["visits"] for entry in result.result["candidates"]) == 128
