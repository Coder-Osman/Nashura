from dataclasses import replace
import hashlib

import pytest
import torch

from native_runner.tests.test_fair_final_learning import small_policy
from native_runner.tests.test_fair_migration_data import complete, context
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_trainer import MigrationTrainer
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.storage import torch_bytes
from tools.diagnose_fair_final_migration import load_diagnostic_student
from tools.fair_migration_diagnostics import diagnose_row, diagnose_validation, phase, summarize


def diagnostic(teacher=None, student=None, *, episode="episode", row="row", owner=0, tick=0, label="WIN"):
    teacher = small_policy(1)[0] if teacher is None else teacher
    student = teacher if student is None else student
    return diagnose_row(teacher, student, torch.tensor([.8, -.1, -.7]), torch.tensor([.8, -.1, -.7]),
        episode_id=semantic_digest(episode), row_id=semantic_digest(row), owner=owner, tick=tick, label=label)


def forced_policy(action):
    plays = torch.zeros(2, 3, 31, dtype=torch.bool)
    plays[:, :, 1:3] = True
    legality = LegalPrefixes([11, 22], plays)
    def logits(prefix):
        values = torch.zeros(legality.mask(prefix).shape)
        target = (int(action.card_id is not None) if not prefix else
            (1 if len(prefix) == 3 else 0) if action.card_id is None else
            legality.card_ids.index(action.card_id) if len(prefix) == 1 else
            action.cell if len(prefix) == 2 else action.delay_slot)
        values[target] = 10.
        return values
    return JointPolicy(logits, legality)


def test_diagnostics_full_joint_kl_matches_enumeration_without_grad_or_rng():
    teacher, teacher_parameters = small_policy(1)
    student, student_parameters = small_policy(2)
    original = semantic_digest(capture_rng())
    row = diagnostic(teacher, student)
    actions = [FullAction()] + [FullAction(teacher.legality.card_ids[card], cell, delay)
        for card, cell, delay in torch.nonzero(teacher.legality.plays).tolist()]
    reference = sum(teacher.log_prob(action).detach().exp() *
        (teacher.log_prob(action).detach() - student.log_prob(action).detach()) for action in actions)
    assert row.metrics["conditional_policy_kl"] == pytest.approx(float(reference), abs=1e-6)
    assert row.metrics["greedy_nll"] == pytest.approx(float(-student.log_prob(row.teacher_action).detach()))
    assert all(parameter.grad is None for parameter in [*teacher_parameters.values(), *student_parameters.values()])
    assert original == semantic_digest(capture_rng())


def test_teacher_act_student_wait_is_mismatch_in_every_act_denominator():
    row = diagnostic(forced_policy(FullAction(11, 0, 1)), forced_policy(FullAction()))
    for name in ("exact_action_match", "teacher_act_exact_match", "mode_match", "card_match",
            "cell_match", "delay_match", "cell_prefix_match"):
        assert row.metrics[name] == 0.
    assert summarize([row])["overall"]["metrics"]["teacher_act_exact_match"]["support_rows"] == 1


def test_component_match_reports_card_prefix_separately():
    row = diagnostic(forced_policy(FullAction(11, 0, 1)), forced_policy(FullAction(22, 0, 1)))
    assert row.metrics["cell_match"] == row.metrics["delay_match"] == 1.
    assert row.metrics["card_match"] == row.metrics["cell_prefix_match"] == 0.


def test_wait_only_population_has_no_act_support_and_no_vacuous_pass():
    legal = LegalPrefixes([11], torch.zeros(1, 1, 31, dtype=torch.bool))
    def logits(prefix):
        assert prefix == ()
        return torch.tensor([0., 1.])
    policy = JointPolicy(logits, legal)
    report = summarize([diagnostic(policy)])
    support = report["overall"]["metrics"]["teacher_act_exact_match"]
    assert support == dict(numerator=0., support_rows=0, row_weighted=None, support_episodes=0, episode_equal=None)
    assert report["fidelity_checks"]["episode_equal"]["teacher_act_exact_match"]["passed"] is None
    assert report["groups"]["teacher_mode"]["WAIT"]["rows"] == 1


def test_episode_equal_exposes_long_game_dominance_and_stratifies_rows():
    teacher = forced_policy(FullAction(11, 0, 1))
    good = diagnostic(teacher, row="a", episode="a", owner=0, tick=0)
    bad = diagnostic(teacher, forced_policy(FullAction()), row="b", episode="b", owner=1, tick=1200)
    other = replace(bad, row_id=semantic_digest("c"), tick=3600)
    report = summarize([good, bad, other])
    metric = report["overall"]["metrics"]["exact_action_match"]
    assert metric["row_weighted"] == pytest.approx(1 / 3)
    assert metric["episode_equal"] == .5
    assert metric["support_rows"] == 3 and metric["support_episodes"] == 2
    assert set(report["groups"]["phase"]) == {"0-60s", "60-180s", "180s+"}
    assert report["groups"]["owner"]["1"]["rows"] == 2
    assert report["fidelity_checks"]["row_weighted"]["exact_action_match"]["passed"] is False
    assert report["training_admissible"] is False


@pytest.mark.parametrize("tick,expected", [(0, "0-60s"), (1199, "0-60s"), (1200, "60-180s"),
    (3599, "60-180s"), (3600, "180s+")])
def test_phase_boundaries_use_fifty_ms_ticks(tick, expected):
    assert phase(tick) == expected


@pytest.mark.parametrize("label,index", [("WIN", 0), ("DRAW", 1), ("LOSS", 2)])
def test_player_relative_terminal_ce_and_brier(label, index):
    row = diagnostic(label=label)
    probability = torch.tensor([.8, -.1, -.7]).double().softmax(-1)
    assert row.metrics["student_terminal_ce"] == pytest.approx(float(-probability[index].log()))
    target = torch.nn.functional.one_hot(torch.tensor(index), 3)
    assert row.metrics["student_terminal_brier"] == pytest.approx(float(((probability - target) ** 2).sum()))
    assert row.metrics["value_teacher_mae"] == row.metrics["wdl_teacher_kl"] == 0.


@pytest.mark.parametrize("bad", ["double", "shape", "nan", "infinity"])
def test_row_rejects_invalid_wdl(bad):
    policy = small_policy(1)[0]
    logits = torch.zeros(3)
    if bad == "double": logits = logits.double()
    elif bad == "shape": logits = logits.unsqueeze(0)
    elif bad == "nan": logits[0] = torch.nan
    else: logits[0] = torch.inf
    with pytest.raises(ValueError):
        diagnose_row(policy, policy, torch.zeros(3), logits, episode_id="a" * 64,
            row_id="b" * 64, owner=0, tick=0, label="WIN")


def test_diagnostic_is_sequential_greedy_rather_than_joint_argmax():
    legal = LegalPrefixes([11], torch.zeros(1, 3, 31, dtype=torch.bool))
    plays = legal.plays.clone()
    plays[0, :, 1:3] = True
    legal = LegalPrefixes([11], plays)
    def logits(prefix):
        return torch.tensor([0., .1]) if not prefix else torch.zeros(legal.mask(prefix).shape)
    policy = JointPolicy(logits, legal)
    row = diagnostic(policy)
    assert row.teacher_action == FullAction(11, 0, 1)
    assert policy.log_prob(FullAction()) > policy.log_prob(row.teacher_action)


@pytest.mark.parametrize("bad", ["temperature", "double", "nan", "masks", "owner_bool", "tick", "label", "sha"])
def test_row_rejects_invalid_diagnostic_inputs(bad):
    teacher, parameters = small_policy(1)
    student = small_policy(2)[0]
    kwargs = dict(episode_id="a" * 64, row_id="b" * 64, owner=0, tick=0, label="WIN")
    if bad == "temperature": teacher.temperatures = (.5, 1., 1., 1.)
    elif bad == "double": teacher = JointPolicy(lambda prefix: parameters[prefix].double(), teacher.legality)
    elif bad == "nan": parameters[()].data[0] = torch.nan
    elif bad == "masks":
        plays = student.legality.plays.clone()
        plays[0, 0, 1] = False
        student = JointPolicy(student.logits, LegalPrefixes([11, 22], plays))
    elif bad == "owner_bool": kwargs["owner"] = True
    elif bad == "tick": kwargs["tick"] = -1
    elif bad == "label": kwargs["label"] = 1
    else: kwargs["row_id"] = "bad"
    with pytest.raises(ValueError):
        diagnose_row(teacher, student, torch.zeros(3), torch.zeros(3), **kwargs)


@pytest.mark.parametrize("bad", ["empty", "duplicate", "missing_metric", "unsupported", "nonfinite", "episode_sha"])
def test_summary_rejects_invalid_support_and_duplicate_rows(bad):
    row = diagnostic(forced_policy(FullAction()))
    rows = [row]
    if bad == "empty": rows = []
    elif bad == "duplicate": rows *= 2
    elif bad == "missing_metric": row.metrics.pop("mode_match")
    elif bad == "unsupported": row.metrics["teacher_act_exact_match"] = 1.
    elif bad == "nonfinite": row.metrics["mode_match"] = float("nan")
    else: rows = [replace(row, episode_id="bad")]
    with pytest.raises(ValueError): summarize(rows)


def test_validation_diagnostics_preserves_models_rng_and_excludes_other_splits(tmp_path, context):
    store, dataset = complete(tmp_path, context)
    teacher, bundle, manifest = context
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config).eval()
    before = semantic_digest(dict(teacher=teacher.state_dict(), student=student.state_dict(), rng=capture_rng()))
    report = diagnose_validation(dataset, student)
    assert report["selected_row_ids"] == dataset.splits["validation"]
    assert report["available_rows"] == 4 and report["complete_validation"] is True
    assert report["engineering_subset"] is False
    assert before == semantic_digest(dict(teacher=teacher.state_dict(), student=student.state_dict(), rng=capture_rng()))
    subset = diagnose_validation(dataset, student, max_rows=2)
    assert subset["complete_validation"] is False and subset["engineering_subset"] is True
    assert subset["selected_row_ids"] == dataset.splits["validation"][::2]
    assert all(not parameter.grad for parameter in student.parameters())


@pytest.mark.parametrize("bad", ["train_mode", "double", "limit", "index_split", "receipt"])
def test_validation_rejects_model_and_population_drift(tmp_path, context, bad):
    store, dataset = complete(tmp_path, context)
    teacher, bundle, manifest = context
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config).eval()
    kwargs = {}
    if bad == "train_mode": student.train()
    elif bad == "double": student.double()
    elif bad == "limit": kwargs["max_rows"] = True
    elif bad == "index_split":
        row_id = dataset.splits["validation"][0]
        match, offset, split = dataset.index[row_id]
        dataset.index[row_id] = match, offset, "train"
    else:
        match = dataset.index[dataset.splits["validation"][0]][0]
        dataset.receipts[match] = "c" * 64
    with pytest.raises(ValueError): diagnose_validation(dataset, student, **kwargs)


def saved_student(tmp_path, context):
    store, dataset = complete(tmp_path / "data", context)
    teacher, bundle, manifest = context
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
        weights=MigrationLossWeights(1., .1, 1.), seed=123, batch_size=1, micro_batch_size=1)
    trainer.update_dataset(dataset)
    path = tmp_path / "checkpoint.pt"
    digest = trainer.save(path)
    return path, digest, dataset, bundle, student


def test_readonly_loader_preserves_checkpoint_network_and_does_not_resume_cursor(tmp_path, context):
    path, digest, dataset, bundle, original = saved_student(tmp_path, context)
    before = path.read_bytes()
    model = load_diagnostic_student(path, expected_sha=digest, dataset=dataset, bundle=bundle, device="cpu")
    assert semantic_digest(model.state_dict()) == semantic_digest(original.state_dict())
    assert all(not module.training for module in model.modules())
    assert path.read_bytes() == before
    assert diagnose_validation(dataset, model)["complete_validation"] is True


@pytest.mark.parametrize("bad", ["file_sha", "semantic_sha", "teacher", "split", "data", "architecture", "double", "nan"])
def test_readonly_loader_rejects_checkpoint_identity_and_network_corruption(tmp_path, context, bad):
    path, digest, dataset, bundle, original = saved_student(tmp_path, context)
    if bad == "file_sha": digest = "a" * 64
    else:
        envelope = torch.load(path, map_location="cpu", weights_only=True)
        payload = envelope["payload"]
        if bad == "semantic_sha": envelope["sha256"] = "a" * 64
        elif bad == "teacher": payload["teacher_network_sha"] = "a" * 64
        elif bad == "split": payload["splits"]["validation"] = payload["splits"]["train"]
        elif bad == "data": payload["identity"]["dataset_sha"] = "a" * 64
        elif bad == "architecture": payload["architecture"]["value_order"] = ["LOSS", "DRAW", "WIN"]
        else:
            name = next(name for name, value in payload["network"].items() if value.is_floating_point())
            if bad == "double": payload["network"][name] = payload["network"][name].double()
            else: payload["network"][name].view(-1)[0] = torch.nan
        if bad != "semantic_sha":
            if bad == "nan":
                payload["network"][name].view(-1)[0] = 0.
                envelope["sha256"] = semantic_digest(payload)
                payload["network"][name].view(-1)[0] = torch.nan
            else: envelope["sha256"] = semantic_digest(payload)
        data = torch_bytes(envelope)
        path = tmp_path / "corrupt.pt"
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError):
        load_diagnostic_student(path, expected_sha=digest, dataset=dataset, bundle=bundle, device="cpu")
