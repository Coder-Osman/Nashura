from dataclasses import replace
import math

import pytest
import torch

from native_runner.training.fair_final.candidates import act_only_noise, structured_candidates
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_final.timing import DecisionClock


def toy_policy():
    mask = torch.zeros(2, 3, 31, dtype=torch.bool)
    mask[0, 0, 1:3] = True
    mask[0, 2, 3] = True
    mask[1, 1, 1:4] = True
    legality = LegalPrefixes([26000021, 26000011], mask)
    parameters = {(): torch.tensor([.3, -.2], requires_grad=True),
                  (1,): torch.tensor([-.7, .9], requires_grad=True)}
    for card in range(2):
        parameters[(1, card)] = torch.tensor([.1, -.4, .8], requires_grad=True)
        for cell in range(3):
            parameters[(1, card, cell)] = torch.linspace(-1 + cell, 1 + card, 31, requires_grad=True)
    return JointPolicy(lambda prefix: parameters[prefix], legality), parameters


def enumerate_actions(policy):
    return [FullAction(), *[FullAction(policy.legality.card_ids[card], cell, delay)
        for card, cell, delay in torch.nonzero(policy.legality.plays).tolist()]]


def test_joint_probability_matches_prefix_reference_and_samples():
    policy, parameters = toy_policy()
    actions = enumerate_actions(policy)
    probabilities = torch.stack([policy.log_prob(action).exp() for action in actions])
    assert float(probabilities.detach().sum()) == pytest.approx(1., abs=1e-6)
    generator = torch.Generator().manual_seed(123)
    counts = dict.fromkeys(actions, 0)
    for ordinal in range(5000):
        action, logp = policy.sample(generator)
        assert torch.equal(logp, policy.log_prob(action))
        counts[action] += 1
    for action, probability in zip(actions, probabilities.detach()):
        assert abs(counts[action] / 5000 - float(probability)) < .035
    (-policy.log_prob(actions[-1])).backward()
    assert parameters[()].grad.abs().sum() > 0
    assert parameters[(1,)].grad.abs().sum() > 0
    assert parameters[(1, 1, 1)].grad.abs().sum() > 0


def test_prefix_masks_remove_empty_branches_and_reserved_zero():
    mask = torch.zeros(2, 3, 31, dtype=torch.bool)
    mask[0, 0, 0] = True
    mask[1, 2, 30] = True
    legality = LegalPrefixes([11, 22], mask)
    assert legality.cards.tolist() == [False, True]
    assert legality.cells[1].tolist() == [False, False, True]
    assert not legality.plays[..., 0].any()
    with pytest.raises(ValueError):
        legality.mask((1, 0))
    with pytest.raises(ValueError):
        FullAction(11, 0, 0)


def test_candidate_projection_preserves_each_card_and_wait_mass():
    policy, _ = toy_policy()
    candidates = structured_candidates(policy, seed=123, maximum=2)
    assert len(candidates) == 3
    assert len({entry.action.key for entry in candidates}) == len(candidates)
    assert sum(entry.prior for entry in candidates) == pytest.approx(1., abs=1e-6)
    assert candidates[0].prior == pytest.approx(float(policy.log_prob(FullAction()).detach().exp()))
    for card, card_id in enumerate(policy.legality.card_ids):
        expected = float((policy.conditionals()[1] + policy.conditionals((1,))[card]).detach().exp())
        assert sum(entry.prior for entry in candidates if entry.action.card_id == card_id) == pytest.approx(expected)
    assert structured_candidates(policy, seed=123, maximum=2) == candidates
    noisy = act_only_noise(candidates, seed=456)
    assert noisy['wait'] == candidates[0].prior
    assert sum(noisy.values()) == pytest.approx(1., abs=1e-6)


def test_conditionally_masked_distribution_is_not_flat_global_truncation():
    old_card = torch.tensor([.5, .5])
    remaining_delay_mass = torch.tensor([.1, .9])
    global_truncated_card = old_card * remaining_delay_mass
    global_truncated_card /= global_truncated_card.sum()
    assert not torch.allclose(old_card, global_truncated_card)
    assert global_truncated_card.tolist() == pytest.approx([.1, .9])


def initial_clock():
    clock = DecisionClock()
    clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda owner, tick: (owner, tick))
    clock.commit({0: FullAction(11, 0, 1), 1: FullAction()}, legal=lambda *args: True)
    return clock


def test_same_tick_due_executes_before_freezing_observations():
    clock = initial_clock()
    order = []

    def adjudicate(plans, tick):
        order.append(('execute', tick))
        assert plans[0].due_tick == tick == 102
        return {plan.identity: 'executed' for plan in plans}

    def freeze(owner, tick):
        order.append(('freeze', owner))
        assert owner not in clock.pending
        return owner, tick

    clock.begin_tick(102, wake_owners=(0, 1), adjudicate_due=adjudicate, freeze=freeze)
    assert order == [('execute', 102), ('freeze', 0), ('freeze', 1)]
    assert [entry['reason'] for entry in clock.evidence] == ['executed', 'preempted']
    assert clock.boundary(0) and clock.boundary(1)


def test_earlier_event_preempts_both_wait_and_act_before_freeze():
    clock = initial_clock()
    clock.begin_tick(101, wake_owners=(0, 1), adjudicate_due=lambda *args: pytest.fail('early execution'),
                     freeze=lambda owner, tick: dict(pending=owner in clock.pending))
    assert not clock.pending
    assert [entry['reason'] for entry in clock.evidence] == ['preempted', 'preempted']


def test_atomic_commit_rejection_and_snapshot_restore():
    clock = initial_clock()
    restored = DecisionClock.restore(clock.capture())
    assert restored.capture() == clock.capture()
    restored.begin_tick(102, wake_owners=(1,), adjudicate_due=lambda plans, tick: {plan.identity: 'rejected' for plan in plans},
                        freeze=lambda owner, tick: (owner, tick))
    assert restored.evidence[0]['reason'] == 'rejected'
    with pytest.raises(ValueError):
        restored.commit({0: FullAction()}, legal=lambda *args: True)
    with pytest.raises(ValueError):
        restored.commit({0: FullAction(), 1: FullAction()}, legal=lambda owner, action: owner == 0)
    assert not restored.pending


def test_wait_expiry_uses_sixty_ticks_and_skipped_deadline_is_rejected():
    clock = DecisionClock()
    clock.begin_tick(0, wake_owners=(0,), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    clock.commit({0: FullAction()}, legal=lambda *args: True)
    with pytest.raises(ValueError):
        clock.begin_tick(61, wake_owners=(), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    clock.begin_tick(60, wake_owners=(), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    assert clock.evidence[-1]['reason'] == 'wait_expired'


def test_full_action_codec_roundtrips_all_executable_slots():
    from native_runner.training.fair_final.codec import ActionCodec

    codec = ActionCodec([11, 22, 33, 44, 55, 66, 77, 88])
    assert codec.decode(0) == FullAction()
    for card in codec.card_ids:
        for cell in range(576):
            for delay in range(1, 31):
                action = FullAction(card, cell, delay)
                assert codec.decode(codec.encode(action)) == action
    for position in range(8 * 576):
        with pytest.raises(ValueError):
            codec.decode(1 + position * 31)


def test_clock_rejects_malformed_deadlines_and_duplicate_restored_identities():
    clock = initial_clock()
    payload = clock.capture()
    payload['pending'][0]['due_tick'] += 1
    with pytest.raises(ValueError):
        DecisionClock.restore(payload)
    payload = clock.capture()
    payload['pending'][1]['identity'] = payload['pending'][0]['identity']
    with pytest.raises(ValueError):
        DecisionClock.restore(payload)
