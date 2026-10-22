"""The skill IL boundary must preserve every pre-existing tensor."""
from dataclasses import replace

import pytest
import torch
import torch.nn.functional as F

from native_runner.tests.test_event_ability_actions import ability_model, ability_frame, context
from native_runner.training.event_ability_il import (
    ABILITY_PARAMETER_NAMES, AbilityILFeatures, ability_il_features, ability_il_loss,
    ability_il_step, frozen_state_digests, make_ability_optimizer, training_state_digest, verify_frozen_state,
)


def features(model, n=6):
    ctx = context(model, ability_frame(n))
    return ability_il_features(model, ctx, old_gate_mask=torch.ones(n, 2, dtype=torch.bool),
                               target=torch.tensor([2, -1] * (n // 2)))


def test_optimizer_is_skill_only_and_old_state_is_bitwise_unchanged():
    model = ability_model()
    before = frozen_state_digests(model)
    optimizer = make_ability_optimizer(model)
    initial_gate = model.ability_gate.weight.detach().clone()
    batch = features(model)
    for _ in range(3):
        assert ability_il_step(model, optimizer, batch)["grad_norm"] > 0
    assert not torch.equal(initial_gate, model.ability_gate.weight)
    assert {name for name, p in model.named_parameters() if p.requires_grad} == ABILITY_PARAMETER_NAMES
    assert all(p.grad is None for name, p in model.named_parameters() if name not in ABILITY_PARAMETER_NAMES)
    verify_frozen_state(model, before)
    with torch.no_grad():
        next(p for n, p in model.named_parameters() if n not in ABILITY_PARAMETER_NAMES).add_(1)
    with pytest.raises(ValueError, match="frozen model state changed"):
        verify_frozen_state(model, before)


def test_loss_is_exact_skill_marginal_and_conditional_source_likelihood():
    model = ability_model()
    make_ability_optimizer(model)
    batch = features(model)
    mask = batch.ability_mask.clone()
    mask[:, 1] = True
    batch = replace(batch, ability_mask=mask)
    gate = torch.cat((batch.old_gate_logits, model.ability_gate(batch.policy_context)), -1)
    gate_logp = gate.log_softmax(-1)
    positive = batch.target >= 0
    source = torch.einsum('bd,bcd->bc', model.ability_query(batch.policy_context),
                          batch.candidate_memory) / model.card_dim ** .5
    expected = -torch.where(positive, gate_logp[:, 2], gate_logp[:, :2].logsumexp(-1)).mean()
    expected += F.cross_entropy(source.masked_fill(~mask, -torch.inf)[positive],
                                batch.target[positive], reduction='sum') / len(positive)
    actual, _ = ability_il_loss(model, batch)
    torch.testing.assert_close(actual, expected)
    actual.backward()
    assert model.ability_query.weight.grad is not None
    assert model.ability_query.weight.grad.abs().sum() > 0


@pytest.mark.parametrize('bad', ['all_parameters', 'duplicate', 'training_mode', 'unfrozen'])
def test_training_rejects_any_broadened_parameter_scope(bad):
    model = ability_model()
    optimizer = make_ability_optimizer(model)
    batch = features(model)
    if bad == 'all_parameters': optimizer = torch.optim.AdamW(model.parameters())
    elif bad == 'duplicate': optimizer.param_groups[0]['params'].append(model.ability_gate.weight)
    elif bad == 'training_mode': model.train()
    else: next(model.backbone.parameters()).requires_grad_(True)
    with pytest.raises(ValueError, match='only the four'):
        ability_il_step(model, optimizer, batch)


@pytest.mark.parametrize('bad', ['illegal_source', 'nan', 'attached', 'no_skill', 'no_alternative'])
def test_invalid_cached_features_are_rejected(bad):
    model = ability_model()
    make_ability_optimizer(model)
    batch = features(model)
    if bad == 'illegal_source': batch = replace(batch, target=torch.ones_like(batch.target))
    elif bad == 'nan': batch.policy_context[0, 0] = float('nan')
    elif bad == 'attached': batch.policy_context.requires_grad_(True)
    elif bad == 'no_skill': batch.ability_mask[:] = False
    else: batch.old_gate_mask[:] = False
    with pytest.raises(ValueError): ability_il_loss(model, batch)


def test_all_negative_batch_still_trains_gate_with_zero_source_loss():
    model = ability_model()
    optimizer = make_ability_optimizer(model)
    batch = features(model)
    batch = replace(batch, target=torch.full_like(batch.target, -1))
    stats = ability_il_step(model, optimizer, batch)
    assert stats['source_loss'] == 0 and stats['positives'] == 0


def test_skill_training_leaves_card_only_inputs_and_action_probabilities_exact():
    from native_runner.tests.test_event_ability_actions import masks
    model = ability_model()
    optimizer = make_ability_optimizer(model)
    frame = ability_frame()
    frame.candidates.mask[:, 2] = False
    with torch.no_grad():
        before = model.select_action(context(model, frame), **masks(model), sample=False)
    ability_il_step(model, optimizer, features(model))
    with torch.no_grad():
        after = model.select_action(context(model, frame), **masks(model), sample=False)
    assert torch.equal(before.action.gate_choice, after.action.gate_choice)
    assert torch.equal(before.log_prob, after.log_prob)


def test_optimizer_digest_survives_copy_and_detects_a_changed_moment():
    from copy import deepcopy
    model = ability_model()
    optimizer = make_ability_optimizer(model)
    ability_il_step(model, optimizer, features(model))
    state = optimizer.state_dict()
    copied = deepcopy(state)
    assert training_state_digest(state) == training_state_digest(copied)
    next(iter(copied['state'].values()))['exp_avg'].add_(1)
    assert training_state_digest(state) != training_state_digest(copied)
