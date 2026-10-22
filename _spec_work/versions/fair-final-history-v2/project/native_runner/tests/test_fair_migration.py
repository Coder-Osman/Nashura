import pytest
import torch

from native_runner.tests.test_fair_final_learning import fixture_source, small_policy
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_migration import MigrationLossWeights, migration_batch_loss, migration_loss
from native_runner.training.fair_search.contracts import semantic_digest


WEIGHTS = MigrationLossWeights(conditional_policy_kl=.7, greedy_nll=.2, wdl_teacher_kl=.3)


def loss_fixture():
    teacher, teacher_parameters = small_policy(1)
    student, student_parameters = small_policy(2)
    teacher_wdl = torch.tensor([[1., -.2, -.7]], requires_grad=True)
    student_wdl = torch.tensor([[-.1, .4, .3]], requires_grad=True)
    return teacher, student, teacher_wdl, student_wdl, teacher_parameters, student_parameters


def test_migration_combines_full_joint_soft_policy_greedy_and_wdl_references():
    teacher, student, teacher_wdl, student_wdl, teacher_parameters, student_parameters = loss_fixture()
    actions = [FullAction(), *[FullAction(teacher.legality.card_ids[card], cell, delay)
        for card, cell, delay in torch.nonzero(teacher.legality.plays).tolist()]]
    policy_kl = sum(teacher.log_prob(action).detach().exp() *
        (teacher.log_prob(action).detach() - student.log_prob(action)) for action in actions)
    greedy = teacher.sample(torch.Generator(), greedy=True)[0]
    greedy_nll = -student.log_prob(greedy)
    teacher_log = teacher_wdl.detach().log_softmax(-1)
    wdl_kl = (teacher_log.exp() * (teacher_log - student_wdl.log_softmax(-1))).sum(-1).mean()
    result = migration_loss([teacher], [student], teacher_wdl, student_wdl, weights=WEIGHTS)
    torch.testing.assert_close(result["conditional_policy_kl"], policy_kl)
    torch.testing.assert_close(result["greedy_nll"], greedy_nll)
    torch.testing.assert_close(result["wdl_teacher_kl"], wdl_kl)
    torch.testing.assert_close(result["loss"], .7*policy_kl+.2*greedy_nll+.3*wdl_kl)
    result["loss"].backward()
    assert result["rows"] == 1
    assert teacher_wdl.grad is None and all(parameter.grad is None for parameter in teacher_parameters.values())
    assert student_parameters[()].grad.abs().sum() > 0 and student_wdl.grad.abs().sum() > 0


def test_migration_averages_decisions_and_preserves_global_rng():
    teacher, student, teacher_wdl, student_wdl, _, _ = loss_fixture()
    before = torch.get_rng_state().clone()
    single = migration_loss([teacher], [student], teacher_wdl, student_wdl, weights=WEIGHTS)
    repeated = migration_loss([teacher]*3, [student]*3,
        teacher_wdl.repeat(3, 1), student_wdl.repeat(3, 1), weights=WEIGHTS)
    for name in ("loss", "conditional_policy_kl", "greedy_nll", "wdl_teacher_kl"):
        torch.testing.assert_close(single[name], repeated[name])
    assert torch.equal(torch.get_rng_state(), before)


def test_migration_student_deployment_temperature_does_not_change_training_objective():
    teacher, student, teacher_wdl, student_wdl, _, parameters = loss_fixture()
    tempered = JointPolicy(parameters.__getitem__, student.legality, temperatures=(.2, 2., .5, 1.7))
    plain = migration_loss([teacher], [student], teacher_wdl, student_wdl, weights=WEIGHTS)
    changed = migration_loss([teacher], [tempered], teacher_wdl, student_wdl, weights=WEIGHTS)
    torch.testing.assert_close(plain["loss"], changed["loss"])


@pytest.mark.parametrize("mutation", ["empty", "rows", "teacher_shape", "student_shape",
    "teacher_dtype", "student_dtype", "nan", "infinity", "temperature", "legality", "weights"])
def test_migration_rejects_invalid_or_incompatible_supervision(mutation):
    teacher, student, teacher_wdl, student_wdl, _, parameters = loss_fixture()
    teachers, students, weights = [teacher], [student], WEIGHTS
    if mutation == "empty": teachers, students = [], []
    elif mutation == "rows": teachers = []
    elif mutation == "teacher_shape": teacher_wdl = teacher_wdl[0]
    elif mutation == "student_shape": student_wdl = student_wdl[:, :2]
    elif mutation == "teacher_dtype": teacher_wdl = teacher_wdl.double()
    elif mutation == "student_dtype": student_wdl = student_wdl.double()
    elif mutation == "nan": teacher_wdl = teacher_wdl.detach().fill_(torch.nan)
    elif mutation == "infinity": student_wdl = student_wdl.detach().fill_(torch.inf)
    elif mutation == "temperature": teacher.temperatures = (.5, 1., 1., 1.)
    elif mutation == "legality":
        plays = student.legality.plays.clone()
        plays[0, 0, 1] = False
        students = [JointPolicy(parameters.__getitem__, LegalPrefixes(student.legality.card_ids, plays))]
    else: weights = None
    with pytest.raises(ValueError):
        migration_loss(teachers, students, teacher_wdl, student_wdl, weights=weights)


@pytest.mark.parametrize("weights", [(0., 0., 0.), (-1., 1., 1.), (1., torch.nan, 1.),
    (1., 1., torch.inf), (True, 1., 1.)])
def test_migration_requires_explicit_valid_weights(weights):
    with pytest.raises(ValueError, match="weights"):
        MigrationLossWeights(*weights)


def test_wait_only_migration_does_not_call_unreachable_act_heads():
    legality = LegalPrefixes([11], torch.zeros(1, 1, 31, dtype=torch.bool))
    def logits(prefix):
        assert not prefix
        return torch.tensor([1., -1.], requires_grad=True)
    policy = JointPolicy(logits, legality)
    result = migration_loss([policy], [policy], torch.zeros(1, 3),
        torch.zeros(1, 3, requires_grad=True), weights=WEIGHTS)
    assert float(result["loss"].detach()) == 0.
    result["loss"].backward()


def test_source_adapter_to_student_update_freezes_teacher_and_staged_encoder():
    torch.manual_seed(105)
    source, bundle = fixture_source()
    source.requires_grad_(False)
    before_source = semantic_digest(source.state_dict())
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=source.policy.backbone.config).eval()
    student.copy_encoder(source)
    student.encoder.requires_grad_(False)
    before_encoder = semantic_digest(student.encoder.state_dict())
    before_heads = semantic_digest(student.policy_head.state_dict())
    before_wdl = semantic_digest(student.wdl_head.state_dict())
    before_trunk = semantic_digest(student.trunk.state_dict())
    frame = _batch(2)
    cards = tuple(reversed(bundle.card_catalog.raw_card_ids))
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    plays[0, 0, 1:3] = True
    plays[1, 1, 2:4] = True
    legalities = [LegalPrefixes(cards, plays), LegalPrefixes(cards, plays.clone())]
    optimizer = torch.optim.AdamW(student.parameters(), lr=1e-4, weight_decay=1e-4)
    result = migration_batch_loss(source, student, frame, legalities, weights=WEIGHTS)
    assert result["rows"] == 2
    result["loss"].backward()
    torch.nn.utils.clip_grad_norm_(student.parameters(), 1.)
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    assert semantic_digest(source.state_dict()) == before_source
    assert all(parameter.grad is None for parameter in source.parameters())
    assert semantic_digest(student.encoder.state_dict()) == before_encoder
    assert semantic_digest(student.policy_head.state_dict()) != before_heads
    assert semantic_digest(student.wdl_head.state_dict()) != before_wdl
    assert semantic_digest(student.trunk.state_dict()) != before_trunk
    student.encoder.requires_grad_(True)
    migration_batch_loss(source, student, frame, legalities, weights=WEIGHTS)["loss"].backward()
    assert any(parameter.grad is not None and parameter.grad.abs().sum() > 0
        for parameter in student.encoder.parameters())
    optimizer.step()
    assert semantic_digest(student.encoder.state_dict()) != before_encoder
    assert semantic_digest(source.state_dict()) == before_source


def test_migration_can_fit_full_joint_and_wdl_targets_on_small_data():
    teacher, student, teacher_wdl, student_wdl, teacher_parameters, student_parameters = loss_fixture()
    optimizer = torch.optim.AdamW([*student_parameters.values(), student_wdl], lr=.03, weight_decay=0.)
    weights = MigrationLossWeights(1., 0., 1.)
    initial = migration_loss([teacher], [student], teacher_wdl, student_wdl, weights=weights)
    initial_policy = float(initial["conditional_policy_kl"].detach())
    initial_wdl = float(initial["wdl_teacher_kl"].detach())
    for step in range(300):
        optimizer.zero_grad(set_to_none=True)
        result = migration_loss([teacher], [student], teacher_wdl, student_wdl, weights=weights)
        result["loss"].backward()
        optimizer.step()
    final = migration_loss([teacher], [student], teacher_wdl, student_wdl, weights=weights)
    assert float(final["conditional_policy_kl"].detach()) < min(.001, initial_policy / 100)
    assert float(final["wdl_teacher_kl"].detach()) < min(.001, initial_wdl / 100)
    assert all(parameter.grad is None for parameter in teacher_parameters.values())
    assert teacher_wdl.grad is None


@pytest.mark.parametrize("mutation", ["training", "gradients", "double", "rows"])
def test_batch_adapter_rejects_unfrozen_teacher_and_wrong_inputs(mutation):
    source, bundle = fixture_source()
    source.requires_grad_(False)
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=source.policy.backbone.config).eval()
    frame = _batch(1)
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    plays[0, 0, 1] = True
    legalities = [LegalPrefixes(bundle.card_catalog.raw_card_ids, plays)]
    if mutation == "training": source.train()
    elif mutation == "gradients": next(source.parameters()).requires_grad_(True)
    elif mutation == "double": student.double()
    else: legalities = []
    with pytest.raises(ValueError):
        migration_batch_loss(source, student, frame, legalities, weights=WEIGHTS)
