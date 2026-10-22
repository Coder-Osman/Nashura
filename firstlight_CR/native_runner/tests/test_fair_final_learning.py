import pytest
import torch
from dataclasses import replace
from types import SimpleNamespace

from native_runner.training.fair_final.learning import conditional_teacher_kl, supervised_loss
from native_runner.training.fair_final.model import ConditionalHeads
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes


def small_policy(seed):
    generator = torch.Generator().manual_seed(seed)
    legal = torch.zeros(2, 3, 31, dtype=torch.bool)
    legal[0, :2, 1:3] = True
    legal[1, 2, 1:4] = True
    parameters = {(): torch.randn(2, generator=generator, requires_grad=True),
        (1,): torch.randn(2, generator=generator, requires_grad=True)}
    for card in range(2):
        parameters[1, card] = torch.randn(3, generator=generator, requires_grad=True)
        for cell in range(3):
            parameters[1, card, cell] = torch.randn(31, generator=generator, requires_grad=True)
    return JointPolicy(parameters.__getitem__, LegalPrefixes([11, 22], legal)), parameters


def test_chain_kl_equals_full_joint_reference_and_detaches_teacher():
    teacher, teacher_parameters = small_policy(1)
    student, student_parameters = small_policy(2)
    actions = [FullAction(), *[FullAction(teacher.legality.card_ids[card], cell, delay)
        for card, cell, delay in torch.nonzero(teacher.legality.plays).tolist()]]
    reference = sum(teacher.log_prob(action).detach().exp()
        * (teacher.log_prob(action).detach() - student.log_prob(action)) for action in actions)
    actual = conditional_teacher_kl(teacher, student, cell_chunk=1)
    torch.testing.assert_close(actual, reference)
    actual.backward()
    assert all(parameter.grad is None for parameter in teacher_parameters.values())
    assert student_parameters[()].grad.abs().sum() > 0


def test_full_fast_losses_have_separate_denominators():
    policy, parameters = small_policy(3)
    targets = [(FullAction(), 32), (FullAction(11, 0, 1), 96)]
    logits = torch.tensor([[.3, .1, -.5]] * 4, requires_grad=True)
    result = supervised_loss([policy] * 4, logits, [targets, None, None, None], torch.tensor([0, 1, 2, 0]))
    reference = -.25 * policy.log_prob(FullAction()) - .75 * policy.log_prob(FullAction(11, 0, 1))
    torch.testing.assert_close(result["policy_loss"], reference)
    torch.testing.assert_close(result["value_loss"], torch.nn.functional.cross_entropy(logits, torch.tensor([0, 1, 2, 0])))
    assert (result["policy_rows"], result["value_rows"]) == (1, 4)
    result["loss"].backward()
    assert parameters[()].grad.abs().sum() > 0 and logits.grad.abs().sum() > 0
    fast = supervised_loss([policy], logits[:1], [None], torch.tensor([0]))
    assert float(fast["policy_loss"].detach()) == 0
    with pytest.raises(ValueError):
        supervised_loss([policy], logits[:1], [[(FullAction(), 32)]], torch.tensor([0]))


def test_neural_heads_condition_on_card_and_position_with_shared_gradients():
    torch.manual_seed(44)
    heads = ConditionalHeads(card_width=8, spatial_width=4)
    shared = torch.randn(512, requires_grad=True)
    cards = torch.randn(2, 8, requires_grad=True)
    spatial = torch.randn(4, 2, 3, requires_grad=True)
    logits = heads.logits_for(shared, cards, spatial)
    assert not torch.allclose(logits((1, 0)), logits((1, 1)))
    assert not torch.allclose(logits((1, 0, 0)), logits((1, 0, 1)))
    legality = LegalPrefixes([11, 22], torch.ones(2, 6, 31, dtype=torch.bool))
    policy = JointPolicy(logits, legality)
    (-policy.log_prob(FullAction(22, 2, 30))).backward()
    assert all(tensor.grad.abs().sum() > 0 for tensor in (shared, cards, spatial))


def test_deployment_temperature_does_not_change_training_log_probability():
    policy, parameters = small_policy(9)
    tempered = JointPolicy(parameters.__getitem__, policy.legality, temperatures=(.3, 2., .7, 1.5))
    action = FullAction(11, 0, 1)
    torch.testing.assert_close(tempered.log_prob(action), policy.log_prob(action))
    assert not torch.allclose(tempered.behavior_log_prob(action), policy.log_prob(action))
    chosen, logp = tempered.sample(torch.Generator().manual_seed(22))
    torch.testing.assert_close(logp, tempered.behavior_log_prob(chosen))


def fixture_source():
    from native_runner.tests.test_training_v4_model import _model
    from native_runner.training.v4.model import UniversalCardPolicyV4
    from native_runner.training.event_model import EventCardPolicyV1
    from native_runner.training.fair_search.model import SearchPolicy

    previous = _model()
    backbone = UniversalCardPolicyV4(previous.catalog, config=replace(previous.config, snapshot_only=True),
        ability_catalog=previous.ability_catalog, entity_archetype_catalog=previous.entity_archetype_catalog,
        effect_catalog=previous.effect_catalog, mechanic_profile_catalog=previous.mechanic_profile_catalog)
    source = SearchPolicy(EventCardPolicyV1(backbone)).eval()
    bundle = SimpleNamespace(card_catalog=backbone.catalog, ability_catalog=backbone.ability_catalog,
        entity_archetype_catalog=backbone.entity_archetype_catalog, effect_catalog=backbone.effect_catalog,
        mechanic_profile_catalog=backbone.mechanic_profile_catalog)
    return source, bundle


def test_full_model_aligns_stable_card_ids_and_updates_shared_encoder():
    from native_runner.tests.test_training_v4_model import _batch
    from native_runner.training.fair_final.model import ConditionalPolicyValue

    source, bundle = fixture_source()
    model = ConditionalPolicyValue(bundle=bundle, encoder_config=source.policy.backbone.config).eval()
    copied = model.copy_encoder(source)
    assert copied
    frame = _batch(1)
    cards = list(reversed(bundle.card_catalog.raw_card_ids))
    legal = LegalPrefixes(cards, torch.ones(8, 576, 31, dtype=torch.bool))
    policies, value = model(frame, [legal])
    shared, encoded = model.encode(frame)
    expected_logits = model.policy_head.logits_for(shared[0], encoded.own_card_memory[0].flip(0), encoded.spatial_memory[0])
    torch.testing.assert_close(policies[0].conditionals((1,)), expected_logits((1,)).log_softmax(-1))
    torch.testing.assert_close(value, model.value_only(frame))
    loss = supervised_loss(policies, value, [[(FullAction(cards[0], 25, 30), 128)]], torch.tensor([0]))
    loss["loss"].backward()
    for module in (model.encoder, model.trunk, model.policy_head, model.wdl_head):
        assert any(parameter.grad is not None and bool(parameter.grad.abs().sum() > 0) for parameter in module.parameters())


def test_source_adapter_preserves_conditional_logits_and_exact_delay_mapping():
    from native_runner.tests.test_training_v4_model import _batch
    from native_runner.training.fair_final.adapter import source_policy

    source, bundle = fixture_source()
    frame = _batch(1)
    cards = tuple(reversed(bundle.card_catalog.raw_card_ids))
    legality = LegalPrefixes(cards, torch.ones(8, 576, 31, dtype=torch.bool))
    adapted, value = source_policy(source, frame, legality)
    context, reference_value = source.encode(frame)
    torch.testing.assert_close(value, reference_value)
    torch.testing.assert_close(adapted.conditionals((1,)), source.policy.card_logits(context)[0].flip(0).log_softmax(-1))
    reference = source.policy.delay_logits(context, torch.tensor([7]), torch.tensor([25]))[0, :31]
    reference = reference.clone()
    reference[0] = -torch.inf
    torch.testing.assert_close(adapted.conditionals((1, 0, 25)), reference.log_softmax(-1))
    assert not torch.isfinite(adapted.conditionals((1, 0, 25))[0])


def test_payment_mask_uses_integer_native_rates_before_command_application():
    from native_runner.training.fair_final.adapter import native_payment_mask

    timeline = SimpleNamespace(elixir_rate=lambda tick: SimpleNamespace(full_bar_ms=28000 if tick < 170 else 14000))
    mask = native_payment_mask(tick=167, elixir_raw=39726, cost_raw=40000, timeline=timeline)
    assert not bool(mask[0]) and not bool(mask[1]) and bool(mask[2])
    exact = native_payment_mask(tick=168, elixir_raw=39904, cost_raw=40000, timeline=timeline)
    assert bool(exact[1])
