from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_protocol import enumerate_actions, toy_policy
from native_runner.training.fair_final.conditioned_plan import ConditionalPlanDistribution, HistoryConditionedOpponent
from native_runner.training.fair_final.history_belief import HistoryReplaySession, IncompatibleHistory
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_final.prior_history import PriorHistoryBeliefFactory
from native_runner.training.fair_final.refill_belief import RefillPriorHistoryBeliefFactory


def conditioned_logp(distribution, action):
    if action.card_id is None:
        return distribution.conditional()[0]
    card = distribution.policy.legality.card_ids.index(action.card_id)
    return (distribution.conditional()[1] + distribution.conditional((1,))[card]
        + distribution.conditional((1, card))[action.cell]
        + distribution.conditional((1, card, action.cell))[action.delay_slot])


@pytest.mark.parametrize("elapsed", [0, 1, 2, 3, 4, 5, 6, 59, 60, 61])
@pytest.mark.parametrize("temperatures", [(1., 1., 1., 1.), (.7, 1.2, .8, 1.6)])
def test_backward_masses_match_full_joint_conditional_reference(elapsed, temperatures):
    original, _ = toy_policy()
    policy = JointPolicy(original.logits, original.legality, temperatures=temperatures)
    allowed = [action for action in enumerate_actions(policy)
        if action.card_id is None or action.delay_slot*2 > elapsed]
    reference = torch.stack([policy.behavior_log_prob(action).detach().double() for action in allowed])
    distribution = ConditionalPlanDistribution(policy, elapsed_ticks=elapsed)
    assert float(distribution.log_mass) == pytest.approx(float(reference.logsumexp(0)), abs=2e-7)
    actual = torch.stack([conditioned_logp(distribution, action) for action in allowed])
    torch.testing.assert_close(actual, reference.log_softmax(0), atol=2e-7, rtol=1e-6)
    first = torch.Generator().manual_seed(812)
    second = torch.Generator().manual_seed(812)
    assert [distribution.sample(first) for _ in range(20)] == [distribution.sample(second) for _ in range(20)]
    assert all(distribution.sample(first) in allowed for _ in range(20))


def extreme_policy(*, wait=True, long_delay=False):
    plays = torch.zeros(1, 1, 31, dtype=torch.bool)
    plays[0, 0, 1] = True
    plays[0, 0, 30] = long_delay
    legality = LegalPrefixes([11], plays, wait=wait)
    parameters = {(): torch.tensor([-2000., 0.]), (1,): torch.zeros(1), (1, 0): torch.zeros(1),
        (1, 0, 0): torch.tensor([0.]*30+[-1000.])}
    return JointPolicy(lambda prefix: parameters[prefix], legality)


def test_extremely_rare_wait_is_conditioned_without_underflow_or_retries():
    distribution = ConditionalPlanDistribution(extreme_policy(), elapsed_ticks=60)
    assert float(distribution.log_mass) == -2000.
    assert distribution.sample(torch.Generator().manual_seed(1)) == FullAction()


def test_extremely_rare_long_delay_is_conditioned_without_underflow():
    distribution = ConditionalPlanDistribution(extreme_policy(wait=False, long_delay=True), elapsed_ticks=59)
    assert float(distribution.log_mass) == -1000.
    assert distribution.sample(torch.Generator().manual_seed(1)) == FullAction(11, 0, 30)


def test_zero_compatible_mass_is_rejected_instead_of_forcing_wait():
    with pytest.raises(IncompatibleHistory, match="no compatible"):
        ConditionalPlanDistribution(extreme_policy(wait=False), elapsed_ticks=60)


def test_deadline_interrupts_partition_construction_and_sampling():
    policy, _ = toy_policy()
    calls = []
    def deadline():
        calls.append(True)
        if len(calls) == 3:
            raise TimeoutError("bounded partition")
    with pytest.raises(TimeoutError, match="bounded partition"):
        ConditionalPlanDistribution(policy, elapsed_ticks=2, check_deadline=deadline)
    distribution = ConditionalPlanDistribution(policy, elapsed_ticks=2)
    calls[:] = [True, True]
    distribution.check_deadline = deadline
    with pytest.raises(TimeoutError, match="bounded partition"):
        distribution.sample(torch.Generator().manual_seed(1))


@pytest.mark.parametrize("elapsed", [-1, True, 2.5])
def test_invalid_cutoff_is_rejected(elapsed):
    with pytest.raises(ValueError, match="nonnegative"):
        ConditionalPlanDistribution(extreme_policy(), elapsed_ticks=elapsed)


def historical_opponent(policy, *, root_tick=160, deployments=()):
    history = SimpleNamespace(opening=SimpleNamespace(owner=0, tick=100),
        root_tick=root_tick, deployments=deployments)
    calls = []
    def predict(information):
        calls.append(information)
        return policy, None
    return HistoryConditionedOpponent(SimpleNamespace(predict=predict), history), calls


def test_history_rejection_loop_accepts_exact_sample_after_one_encoding():
    policy = extreme_policy()
    conditioned, calls = historical_opponent(policy)
    session = object.__new__(HistoryReplaySession)
    session.opponent, session.seed = conditioned, 17
    info = SimpleNamespace(owner=1, tick=100, legality=policy.legality)
    assert session._opponent_action(info, conditioned.history) == FullAction()
    assert calls == [info]
    assert conditioned.profile["calls"] == 1
    assert conditioned.profile["minimum_log_mass"] == -2000.


def test_public_tick_is_strict_and_only_public_history_conditions_sample():
    policy = extreme_policy(wait=False, long_delay=True)
    conditioned, calls = historical_opponent(policy, root_tick=200,
        deployments=(SimpleNamespace(tick=159, owner=0),))
    info = SimpleNamespace(owner=1, tick=100, legality=policy.legality)
    assert conditioned.sample(info, torch.Generator().manual_seed(17)) == FullAction(11, 0, 30)
    assert calls == [info]
    conditioned.history.deployments = (SimpleNamespace(tick=160, owner=0),)
    assert conditioned.sample(info, torch.Generator().manual_seed(17)) == FullAction(11, 0, 1)
    assert conditioned.profile["zero_cutoff_mass"] == 1


@pytest.mark.parametrize("seed", [0, 3, 17, 29])
def test_history_mixture_matches_seeded_reference_and_encodes_once(seed):
    policy, _ = toy_policy()
    conditioned, calls = historical_opponent(policy, root_tick=104)
    info = SimpleNamespace(owner=1, tick=100, legality=policy.legality)
    reference = torch.Generator().manual_seed(seed)
    if float(torch.rand((), generator=reference)) < .125:
        expected, _ = policy.sample(reference)
    else:
        expected = ConditionalPlanDistribution(policy, elapsed_ticks=4).sample(reference)
    assert conditioned.sample(info, torch.Generator().manual_seed(seed)) == expected
    assert calls == [info]


def test_full_policy_branch_preserves_short_due_actions_at_public_tick():
    policy = extreme_policy()
    conditioned, calls = historical_opponent(policy, root_tick=102,
        deployments=(SimpleNamespace(tick=102, owner=0),))
    info = SimpleNamespace(owner=1, tick=100, legality=policy.legality)
    assert conditioned.sample(info, torch.Generator().manual_seed(3)) == FullAction(11, 0, 1)
    assert conditioned.profile["full_policy"] == 1
    assert conditioned.profile["cutoff_conditioned"] == 0
    assert calls == [info]


@pytest.mark.parametrize("seed", [3, 17])
def test_public_guidance_is_a_mixture_not_an_exclusive_forced_action(seed):
    policy = extreme_policy(long_delay=True)
    conditioned, calls = historical_opponent(policy)
    info = SimpleNamespace(owner=1, tick=100, legality=policy.legality)
    guided = FullAction(11, 0, 30)
    result = conditioned.sample_history(info, torch.Generator().manual_seed(seed), public_action=guided)
    assert result == (FullAction(11, 0, 1) if seed == 3 else guided)
    assert len(calls) == (1 if seed == 3 else 0)


def test_history_proposal_deadline_reaches_partition_and_does_not_record_failed_call():
    policy, _ = toy_policy()
    conditioned, calls = historical_opponent(policy, root_tick=104)
    info = SimpleNamespace(owner=1, tick=100, legality=policy.legality)
    before = dict(conditioned.profile)
    checkpoints = []
    def deadline():
        checkpoints.append(True)
        if len(checkpoints) == 5:
            raise TimeoutError("proposal partition deadline")
    with pytest.raises(TimeoutError, match="partition deadline"):
        conditioned.sample_history(info, torch.Generator().manual_seed(17), check_deadline=deadline)
    assert calls == [info] and conditioned.profile == before


@pytest.mark.parametrize("owner,tick", [(0, 100), (1, 99), (1, 160), (1, 161)])
def test_bound_history_adapter_rejects_wrong_player_or_stale_history(owner, tick):
    conditioned, calls = historical_opponent(extreme_policy())
    with pytest.raises(ValueError, match="bound historical"):
        conditioned.sample(SimpleNamespace(owner=owner, tick=tick), torch.Generator().manual_seed(17))
    assert not calls


def test_factory_wraps_only_replay_opponent_and_preserves_rollout_model(monkeypatch):
    original = object()
    session = SimpleNamespace(opponent=original)
    monkeypatch.setattr(PriorHistoryBeliefFactory, "create", lambda *args, **kwargs: session)
    factory = object.__new__(RefillPriorHistoryBeliefFactory)
    factory.opponent = original
    history = object()
    assert factory.create(history, seed=7) is session
    assert factory.opponent is original
    assert session.opponent.opponent is original
    assert session.opponent.history is history
