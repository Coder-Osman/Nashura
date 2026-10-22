from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
from types import ModuleType

import pytest
import torch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2] / "firstlight_CR"))

from native_runner.tests.test_fair_final_learning import fixture_source, small_policy
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.fair_final.adapter import source_policy as source_reference
from native_runner.training.fair_final.learning import conditional_teacher_kl as reference_kl
from native_runner.training.fair_final.model import ConditionalHeads, ConditionalPolicyValue
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_migration import MigrationLossWeights, migration_batch_loss
from native_runner.training.fair_search.contracts import semantic_digest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


batch = load("native_runner.training.fair_final.batched_migration", ROOT / "batched_migration.py")
builder = load("batch_payload_builder", ROOT / "build_payload.py")
PARENT = ROOT.parent / "fair-final-migration-cache-v1/native-deterministic-round0-1792647014623244000"


@pytest.fixture(scope="module")
def payload():
    if not (PARENT / "payload.tar.gz").exists():
        pytest.skip("private frozen deployment attachment is required for packaging checks")
    return builder.build(PARENT)


@pytest.fixture(scope="module")
def optimized():
    if (PARENT / "payload.tar.gz").exists():
        package, _, _ = builder.build(PARENT)
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            source = archive.extractfile(builder.MIGRATION).read()
    else:
        source = builder.transform_migration((ROOT.parents[2] / builder.MIGRATION).read_text()).encode()
    module = ModuleType("native_runner.training.batched_loss_test")
    module.__package__ = "native_runner.training"
    sys.modules[module.__name__] = module
    exec(compile(source, builder.MIGRATION, "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("chunk", [1, 2, 64, 576])
@pytest.mark.parametrize("temperatures", [(1., 1., 1., 1.), (.2, 2., .5, 1.7)])
def test_exact_support_chain_kl_matches_full_joint_and_detaches_teacher(chunk, temperatures):
    teacher, teacher_parameters = small_policy(10)
    student, student_parameters = small_policy(11)

    def policy(original, parameters, temps):
        return batch.DelayBatchPolicy(parameters.__getitem__, original.legality,
            lambda card, cells: torch.stack([parameters[1, card, cell] for cell in cells]), temperatures=temps)

    teachers = policy(teacher, teacher_parameters, (1., 1., 1., 1.))
    students = policy(student, student_parameters, temperatures)
    actual = batch.conditional_teacher_kl(teachers, students, cell_chunk=chunk)
    actions = [FullAction(), *[FullAction(teacher.legality.card_ids[card], cell, delay)
        for card, cell, delay in torch.nonzero(teacher.legality.plays).tolist()]]
    expected = sum(teacher.log_prob(action).detach().exp() *
        (teacher.log_prob(action).detach() - student.log_prob(action)) for action in actions)
    torch.testing.assert_close(actual, expected, atol=2e-6, rtol=2e-5)
    actual.backward()
    assert all(parameter.grad is None for parameter in teacher_parameters.values())
    assert all(parameter.grad is None or bool(torch.isfinite(parameter.grad).all())
        for parameter in student_parameters.values())
    assert float(student_parameters[()].grad.abs().sum()) > 0


def test_wait_only_never_calls_unreachable_heads():
    legality = LegalPrefixes([11], torch.zeros(1, 576, 31, dtype=torch.bool))

    def delay(*args):
        raise AssertionError("unreachable ACT head")

    def logits(prefix):
        assert prefix == ()
        return torch.tensor([1., -1.], requires_grad=True)

    policy = batch.DelayBatchPolicy(logits, legality, delay)
    result = batch.conditional_teacher_kl(policy, policy)
    assert result.item() == 0
    result.backward()


@pytest.mark.parametrize("chunk", [1, 2, 64])
def test_legal_reductions_preserve_sequential_fp32_accumulation(chunk):
    teacher, teacher_parameters = small_policy(10)
    student, student_parameters = small_policy(11)
    teachers = batch.DelayBatchPolicy(teacher_parameters.__getitem__, teacher.legality,
        lambda card, cells: torch.stack([teacher_parameters[1, card, cell] for cell in cells]))
    students = batch.DelayBatchPolicy(student_parameters.__getitem__, student.legality,
        lambda card, cells: torch.stack([student_parameters[1, card, cell] for cell in cells]))
    expected = reference_kl(teacher, student, cell_chunk=chunk)
    actual = batch.conditional_teacher_kl(teachers, students, cell_chunk=chunk)
    torch.testing.assert_close(actual, expected, atol=0, rtol=0)
    parameters = tuple(student_parameters.values())
    expected_gradients = torch.autograd.grad(expected, parameters, allow_unused=True)
    actual_gradients = torch.autograd.grad(actual, parameters, allow_unused=True)
    for expected_gradient, actual_gradient in zip(expected_gradients, actual_gradients):
        if expected_gradient is None:
            assert actual_gradient is None
        else:
            torch.testing.assert_close(actual_gradient, expected_gradient, atol=0, rtol=0)


@pytest.mark.parametrize("mutation", ["zero_chunk", "bool_chunk", "cards", "plays", "wait", "nan", "shape"])
def test_invalid_contracts_rejected(mutation):
    teacher, parameters = small_policy(5)
    policy = batch.DelayBatchPolicy(parameters.__getitem__, teacher.legality,
        lambda card, cells: torch.stack([parameters[1, card, cell] for cell in cells]))
    student = deepcopy(policy)
    chunk = 64
    if mutation == "zero_chunk": chunk = 0
    elif mutation == "bool_chunk": chunk = True
    elif mutation == "cards": student.legality.card_ids = (22, 11)
    elif mutation == "plays": student.legality.plays[0, 0, 1] = False
    elif mutation == "wait": student.legality.modes[0] = False
    elif mutation == "nan": student.delay_logits = lambda card, cells: torch.full((len(cells), 31), torch.nan)
    else: student.delay_logits = lambda card, cells: torch.zeros(len(cells), 30)
    with pytest.raises(ValueError):
        batch.conditional_teacher_kl(policy, student, cell_chunk=chunk)


def test_standard_joint_policy_fallback_preserves_reference():
    teacher, _ = small_policy(3)
    student, _ = small_policy(4)
    assert torch.equal(batch.conditional_teacher_kl(teacher, student), reference_kl(teacher, student))


def legalities(bundle, count):
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    plays[0, [0, 2, 33, 122, 575], 1:31:2] = True
    plays[2, [4, 22, 101], 3:21] = True
    plays[5, [2, 255], 29:31] = True
    cards = tuple(reversed(bundle.card_catalog.raw_card_ids))
    return [LegalPrefixes(cards, plays) for _ in range(count)]


def test_teacher_all_legal_conditionals_greedy_and_wdl_match_reference():
    torch.manual_seed(20261022)
    source, bundle = fixture_source()
    source.requires_grad_(False)
    frame = _batch(1)
    legality = legalities(bundle, 1)[0]
    expected, expected_wdl = source_reference(source, frame, legality)
    actual, actual_wdl = batch.source_policy(source, frame, legality)
    before = semantic_digest(source.state_dict())
    for prefix in [(), (1,), *[(1, card) for card in torch.where(legality.cards)[0].tolist()]]:
        torch.testing.assert_close(actual.conditionals(prefix), expected.conditionals(prefix), atol=2e-6, rtol=2e-5)
    for card in torch.where(legality.cards)[0].tolist():
        cells = torch.where(legality.cells[card])[0].tolist()
        delays = actual.delay_conditionals(card, cells)
        for index, cell in enumerate(cells):
            torch.testing.assert_close(delays[index], expected.conditionals((1, card, cell)), atol=2e-6, rtol=2e-5)
            assert not torch.isfinite(delays[index, 0])
    torch.testing.assert_close(actual_wdl, expected_wdl, atol=2e-6, rtol=2e-5)
    generator = torch.Generator().manual_seed(77)
    assert actual.sample(generator, greedy=True)[0] == expected.sample(generator, greedy=True)[0]
    assert semantic_digest(source.state_dict()) == before


@pytest.mark.parametrize("frozen", [False, True])
def test_full_model_losses_gradients_and_adamw_match_reference(optimized, frozen):
    torch.manual_seed(123)
    teacher, bundle = fixture_source()
    teacher.requires_grad_(False)
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config).eval()
    student.encoder.requires_grad_(not frozen)
    reference = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config).eval()
    reference.load_state_dict(student.state_dict())
    reference.encoder.requires_grad_(not frozen)
    frame = _batch(2)
    masks = legalities(bundle, 2)
    before_teacher = semantic_digest(teacher.state_dict())
    before_encoder = semantic_digest(student.encoder.state_dict())
    before_rng = torch.get_rng_state().clone()
    weights = dict(conditional_policy_kl=.7, greedy_nll=.2, wdl_teacher_kl=.3)
    expected = migration_batch_loss(teacher, reference, frame, masks, weights=MigrationLossWeights(**weights))
    actual = optimized.migration_batch_loss(teacher, student, frame, masks,
        weights=optimized.MigrationLossWeights(**weights))
    for name in ("loss", "conditional_policy_kl", "greedy_nll", "wdl_teacher_kl"):
        torch.testing.assert_close(actual[name], expected[name], atol=2e-6, rtol=2e-5)
    expected["loss"].backward()
    actual["loss"].backward()
    for (name, parameter), (other_name, other) in zip(student.named_parameters(), reference.named_parameters()):
        assert name == other_name and (parameter.grad is None) == (other.grad is None)
        if parameter.grad is not None:
            torch.testing.assert_close(parameter.grad, other.grad, atol=2e-5, rtol=2e-4, msg=name)
    for model in (student, reference):
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
    optimizers = [torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4) for model in (student, reference)]
    for optimizer in optimizers:
        optimizer.step()
    for (name, parameter), (_, other) in zip(student.named_parameters(), reference.named_parameters()):
        torch.testing.assert_close(parameter, other, atol=3e-6, rtol=2e-5, msg=name)
        for key in optimizers[0].state[parameter]:
            torch.testing.assert_close(optimizers[0].state[parameter][key], optimizers[1].state[other][key],
                atol=2e-5, rtol=2e-4, msg=name + ":" + key)
    assert torch.equal(torch.get_rng_state(), before_rng)
    assert semantic_digest(teacher.state_dict()) == before_teacher
    assert all(parameter.grad is None for parameter in teacher.parameters())
    assert (semantic_digest(student.encoder.state_dict()) == before_encoder) == frozen


def test_batch_head_gradient_to_shared_cards_and_spatial():
    torch.manual_seed(71)
    head = ConditionalHeads(card_width=8, spatial_width=4)
    shared = torch.randn(512, requires_grad=True)
    cards = torch.randn(2, 8, requires_grad=True)
    spatial = torch.randn(4, 2, 3, requires_grad=True)
    legality = LegalPrefixes([11, 22], torch.ones(2, 6, 31, dtype=torch.bool))
    actual = batch.student_policy(head, shared, cards, spatial, legality)
    original = JointPolicy(head.logits_for(shared, cards, spatial), legality)
    for card in range(2):
        rows = actual.delay_conditionals(card, list(range(6)))
        for cell in range(6):
            torch.testing.assert_close(rows[cell], original.conditionals((1, card, cell)), atol=2e-6, rtol=2e-5)
    (-actual.delay_conditionals(1, [0, 2, 5])[:, 1:].mean()).backward()
    assert all(tensor.grad.abs().sum() > 0 for tensor in (shared, cards, spatial))


@pytest.mark.parametrize("frozen", [False, True])
def test_student_dispatch_preserves_sequential_encoder_backward(frozen):
    torch.manual_seed(20261022)
    teacher, bundle = fixture_source()
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config).eval()
    student.encoder.requires_grad_(not frozen)
    frame = _batch(1)
    masks = legalities(bundle, 1)
    actual, actual_wdl = batch.student_policies(student, frame, masks)
    expected, expected_wdl = student(frame, masks)
    assert isinstance(actual[0], batch.DelayBatchPolicy) == frozen
    torch.testing.assert_close(actual_wdl, expected_wdl, atol=0, rtol=0)
    parameters = tuple(parameter for parameter in student.parameters() if parameter.requires_grad)
    target = FullAction(masks[0].card_ids[0], 33, 3)
    actual_loss = -actual[0].log_prob(target) + actual_wdl.sum()
    expected_loss = -expected[0].log_prob(target) + expected_wdl.sum()
    actual_gradients = torch.autograd.grad(actual_loss, parameters, allow_unused=True)
    expected_gradients = torch.autograd.grad(expected_loss, parameters, allow_unused=True)
    for actual_gradient, expected_gradient in zip(actual_gradients, expected_gradients):
        if expected_gradient is None:
            assert actual_gradient is None
        else:
            torch.testing.assert_close(actual_gradient, expected_gradient, atol=0, rtol=0)


def test_payload_only_changes_explicit_sources_and_binds_checkpoint_implementation(payload):
    package, identity, changed = payload
    assert set(changed) == {builder.MODULE, builder.MIGRATION, builder.SERIES}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    assert {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != "deployment.json"} == identity
    assert json.loads(files["deployment.json"]) == identity
    assert "batched_migration.py" in files[builder.SERIES].decode()
    assert files[builder.MODULE] == (ROOT / "batched_migration.py").read_bytes()


def test_payload_rejects_parent_identity_mutation(tmp_path):
    if not (PARENT / "launch.json").exists():
        pytest.skip("private frozen deployment attachment is required for tamper checks")
    launch = json.loads((PARENT / "launch.json").read_text())
    launch["source_sha256"][builder.MIGRATION] = "0" * 64
    (tmp_path / "launch.json").write_text(json.dumps(launch))
    (tmp_path / "payload.tar.gz").write_bytes((PARENT / "payload.tar.gz").read_bytes())
    with pytest.raises(ValueError, match="deployment"):
        builder.build(tmp_path)


def test_native_probe_pins_reference_and_preserves_training_configuration(monkeypatch):
    if not (PARENT / "payload.tar.gz").exists():
        pytest.skip("private frozen deployment attachment is required for packaging checks")
    with monkeypatch.context() as context:
        context.setitem(sys.modules, "build_payload", builder)
        launcher = load("batch_native_launcher", ROOT / "start_native.py")
    package, identity, changes = launcher.package_probe(PARENT)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    with tarfile.open(PARENT / "payload.tar.gz") as archive:
        assert files["reference_migration.py"] == archive.extractfile(builder.MIGRATION).read()
        for name in ("private-plan.json", "loss-weights.json", "selection-config.json"):
            assert files[name] == archive.extractfile(name).read()
    assert set(changes) == {builder.MODULE, builder.MIGRATION, builder.SERIES,
        "reference_migration.py", launcher.PROBE}
    assert identity == json.loads(files["deployment.json"])
    assert all(hashlib.sha256(files[name]).hexdigest() == digest for name, digest in identity.items())


def test_probe_metrics_accept_integer_row_count_and_detach_trainable_loss():
    probe = load("batch_native_probe_test", ROOT / "probe_native_batch.py")
    metrics = probe.numeric_metrics(dict(loss=torch.tensor(2., requires_grad=True), rows=1))
    assert metrics == dict(loss=2., rows=1.)


def test_probe_update_mismatch_preserves_strict_threshold_and_gradient_evidence(tmp_path):
    probe = load("batch_native_probe_diagnostic_test", ROOT / "probe_native_batch.py")
    baseline = torch.nn.Parameter(torch.tensor([0., 1.]))
    batched = torch.nn.Parameter(torch.tensor([4e-6, 1.]))
    baseline.grad = torch.tensor([1e-9, .2])
    batched.grad = torch.tensor([-1e-9, .2])
    output = tmp_path / "mismatch.json"
    with pytest.raises(AssertionError):
        probe.check_parameter_update(baseline, batched, name="encoder.weight", output=output,
            context=dict(trial=0))
    report = json.loads(output.read_text())
    assert report["count"] == 1 and report["flat_indices"] == [0]
    assert report["baseline_gradient"][0] > 0 > report["batched_gradient"][0]
    assert report["context"] == dict(trial=0)
