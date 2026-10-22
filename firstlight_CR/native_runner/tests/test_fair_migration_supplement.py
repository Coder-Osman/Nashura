from copy import deepcopy
import hashlib
import json

import pytest
import torch

from native_runner.tests.test_fair_migration_collect import ScenarioRollout, scenario
from native_runner.tests.test_fair_migration_data import complete, context
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_data import MigrationStore
from native_runner.training.fair_migration_trainer import MigrationTrainer
from native_runner.training.fair_search.contracts import semantic_digest
from tools.fair_migration_supplement import (collect_supplement_episode, load_supplement_student,
    validate_supplement_round)


STUDENT_SHA = "e" * 64


def paired_round(context, round_number):
    parent = deepcopy(context[2])
    parent["round"] = round_number - 1
    if parent["round"]:
        for task in parent["matches"].values():
            task["behavior_checkpoint_shas"][0] = "d" * 64
    current = deepcopy(parent)
    current["round"] = round_number
    current["matches"] = {}
    source = current["identity"]["source_a_sha"]
    for owner in (0, 1):
        models = [source, source]
        models[owner] = STUDENT_SHA
        current["matches"][semantic_digest([round_number, owner])] = dict(
            pair_seed_id=semantic_digest([round_number, "new-pair"]), split="train",
            behavior_checkpoint_shas=models)
    return current, parent


@pytest.mark.parametrize("round_number", [1, 2])
def test_supplement_round_requires_one_student_and_complete_swapped_pairs(context, round_number):
    current, parent = paired_round(context, round_number)
    validate_supplement_round(current, parent, STUDENT_SHA)


@pytest.mark.parametrize("bad", ["round", "identity", "cards", "namespace", "match_overlap",
    "pair_overlap", "missing_seat", "same_seat", "extra_seat", "mixed_student", "source_student", "split"])
def test_supplement_round_rejects_cross_identity_or_incomplete_pairs(context, bad):
    current, parent = paired_round(context, 1)
    tasks = list(current["matches"].values())
    student_sha = STUDENT_SHA
    if bad == "round":
        current["round"] = 2
    elif bad == "identity":
        current["identity"]["engine_protocol_sha"] = "c" * 64
    elif bad == "cards":
        current["card_ids"].reverse()
    elif bad == "namespace":
        current["namespace_sha"] = "c" * 64
    elif bad == "match_overlap":
        current["matches"][next(iter(parent["matches"]))] = current["matches"].pop(next(iter(current["matches"])))
    elif bad == "pair_overlap":
        for task in tasks:
            task["pair_seed_id"] = next(iter(parent["matches"].values()))["pair_seed_id"]
    elif bad == "missing_seat":
        current["matches"].pop(next(iter(current["matches"])))
    elif bad == "same_seat":
        tasks[1]["behavior_checkpoint_shas"] = list(tasks[0]["behavior_checkpoint_shas"])
    elif bad == "extra_seat":
        current["matches"]["a" * 64] = deepcopy(tasks[0])
    elif bad == "mixed_student":
        tasks[1]["behavior_checkpoint_shas"][1] = "d" * 64
    elif bad == "source_student":
        student_sha = current["identity"]["source_a_sha"]
    elif bad == "split":
        tasks[1]["split"] = "validation"
    with pytest.raises(ValueError):
        validate_supplement_round(current, parent, student_sha)


class SupplementRollout(ScenarioRollout):
    def __init__(self, environment, **kwargs):
        super().__init__(environment, **kwargs)
        environment.rollout = self

    def freeze_due_information(self):
        clock = self.runtime.clock
        plays = torch.zeros(8, 576, 31, dtype=torch.bool)
        plays[0, 0, 1] = True
        return {owner: DecisionInformation(owner, clock.tick, self.environment.frames[owner],
            LegalPrefixes(self.environment.cards, plays)) for owner in sorted(clock.ready)}


def supplement(context, *, round_number=1, student_owner=1):
    teacher, bundle, source_manifest = context
    manifest = deepcopy(source_manifest)
    manifest["round"] = round_number
    for task in manifest["matches"].values():
        task["behavior_checkpoint_shas"][student_owner] = STUDENT_SHA
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    with torch.no_grad():
        student.policy_head.mode[-1].weight.zero_()
        student.policy_head.mode[-1].bias.copy_(torch.tensor([-100., 100.]))
    student.eval().requires_grad_(False)
    return manifest, student


def run(store, context, student, environment, *, attempt_id=1, **kwargs):
    return collect_supplement_episode(environment, teacher=context[0], student=student,
        student_checkpoint_sha=STUDENT_SHA, student_network_sha=semantic_digest(student.state_dict()),
        store=store, match_id=semantic_digest("train"), attempt_id=attempt_id, execution_seed=73,
        rollout_factory=SupplementRollout, **kwargs)


@pytest.mark.parametrize("round_number", [1, 2])
@pytest.mark.parametrize("student_owner", [0, 1])
def test_supplement_both_rounds_and_seats_keep_behavior_separate_from_A(tmp_path, context,
        round_number, student_owner, monkeypatch):
    manifest, student = supplement(context, round_number=round_number, student_owner=student_owner)
    store = MigrationStore(tmp_path, manifest=manifest)
    environment = scenario(context)
    observed = []

    def before_student(module, inputs):
        clock = environment.rollout.runtime.clock
        assert clock.ready == {0, 1} and not clock.pending
        assert inputs[0] is environment.frames[student_owner]
        observed.append(clock.tick)

    handle = student.register_forward_pre_hook(before_student)
    monkeypatch.setattr(torch, "multinomial", lambda *args, **kwargs: pytest.fail("greedy migration sampled"))
    student_sha = semantic_digest(student.state_dict())
    teacher_sha = semantic_digest(context[0].state_dict())
    rng_sha = semantic_digest(capture_rng())
    report = run(store, context, student, environment)
    handle.remove()
    episode, _ = store.load(semantic_digest("train"))
    assert observed == [100, 101]
    assert report["round"] == round_number and report["student_owner"] == student_owner
    assert report["rows_by_owner"] == {"0": 2, "1": 2} and report["training_admissible"] is False
    assert report["lifecycle"] == {"preempted": 2, "terminal_expired": 2}
    for row in episode["rows"]:
        assert row["teacher_action"]["card_id"] is None
        assert (row["behavior_action"]["card_id"] is not None) == (row["owner"] == student_owner)
        assert row["wdl"] == ["WIN", "LOSS"][row["owner"]]
    assert rng_sha == semantic_digest(capture_rng())
    assert student_sha == semantic_digest(student.state_dict())
    assert teacher_sha == semantic_digest(context[0].state_dict())


@pytest.mark.parametrize("bad", ["rejected", "failed", "truncated", "views", "not_finalized"])
def test_supplement_bad_game_is_quarantined_without_publication(tmp_path, context, bad):
    manifest, student = supplement(context)
    store = MigrationStore(tmp_path, manifest=manifest)
    environment = scenario(context, bad)
    if bad == "not_finalized":
        environment.raw_observation["finalized"] = False
    with pytest.raises(ValueError):
        run(store, context, student, environment)
    assert not list((tmp_path / "completed").glob("*.json"))
    quarantine = tmp_path / "attempts" / semantic_digest("train") / "1/quarantine.json"
    assert json.loads(quarantine.read_text())["published"] is False


def test_supplement_retry_and_duplicate_publish_accept_one_success(tmp_path, context):
    manifest, student = supplement(context)
    store = MigrationStore(tmp_path, manifest=manifest)
    with pytest.raises(ValueError):
        run(store, context, student, scenario(context, "rejected"))
    success = run(store, context, student, scenario(context), attempt_id=2)
    duplicate = run(store, context, student, scenario(context), attempt_id=3)
    assert success["publication"]["status"] == "published"
    assert duplicate["publication"]["status"] == "duplicate"
    assert len(list((tmp_path / "completed").glob("*.json"))) == 1
    assert store.load(semantic_digest("train"))[0]["attempt_id"] == 2


@pytest.mark.parametrize("seconds", [0., -1., float("nan"), float("inf"), True])
def test_supplement_invalid_budget_is_rejected_before_attempt(tmp_path, context, seconds):
    manifest, student = supplement(context)
    store = MigrationStore(tmp_path, manifest=manifest)
    with pytest.raises(ValueError, match="positive finite"):
        run(store, context, student, scenario(context), wall_seconds=seconds)
    assert not (tmp_path / "attempts").exists()


@pytest.mark.parametrize("bad", ["round_zero", "checkpoint", "train_mode", "requires_grad", "network_sha"])
def test_supplement_identity_and_frozen_model_fail_before_attempt(tmp_path, context, bad):
    manifest, student = supplement(context)
    if bad == "round_zero":
        manifest = deepcopy(context[2])
    if bad == "checkpoint":
        manifest["matches"][semantic_digest("train")]["behavior_checkpoint_shas"][1] = "d" * 64
    if bad == "train_mode":
        student.train()
    if bad == "requires_grad":
        student.requires_grad_(True)
    store = MigrationStore(tmp_path, manifest=manifest)
    with pytest.raises(ValueError):
        collect_supplement_episode(scenario(context), teacher=context[0], student=student,
            student_checkpoint_sha=STUDENT_SHA,
            student_network_sha="f" * 64 if bad == "network_sha" else semantic_digest(student.state_dict()),
            store=store, match_id=semantic_digest("train"), attempt_id=1, execution_seed=73,
            rollout_factory=SupplementRollout)
    assert not (tmp_path / "attempts").exists()


def test_supplement_timeout_does_not_publish_partial_game(tmp_path, context):
    manifest, student = supplement(context)
    store = MigrationStore(tmp_path, manifest=manifest)
    times = iter(range(100))
    with pytest.raises(TimeoutError):
        run(store, context, student, scenario(context), wall_seconds=.5, monotonic=lambda: float(next(times)))
    assert not list((tmp_path / "completed").glob("*.json"))


@pytest.mark.parametrize("target", ["student", "teacher"])
def test_supplement_model_mutation_quarantines_whole_game(tmp_path, context, target):
    manifest, student = supplement(context)
    store = MigrationStore(tmp_path, manifest=manifest)

    def mutate(progress):
        model = student if target == "student" else context[0]
        with torch.no_grad():
            next(model.parameters()).add_(.001)

    with pytest.raises(ValueError, match="unchanged frozen"):
        run(store, context, student, scenario(context), progress=mutate)
    assert not list((tmp_path / "completed").glob("*.json"))


@pytest.mark.parametrize("bad", [None, "checksum", "dataset", "splits", "teacher"])
def test_supplement_checkpoint_loader_preserves_rng_and_checks_provenance(tmp_path, context, bad):
    _, dataset = complete(tmp_path / "parent", context)
    teacher, bundle, _ = context
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
        weights=MigrationLossWeights(1., .1, 1.), seed=991)
    checkpoint = tmp_path / "student.pt"
    expected_sha = trainer.save(checkpoint)
    original_sha = expected_sha
    expected_network = semantic_digest(student.state_dict())
    if bad == "dataset":
        dataset.identity["dataset_sha"] = "c" * 64
    elif bad == "splits":
        dataset.splits["train"].reverse()
    elif bad == "teacher":
        with torch.no_grad():
            next(teacher.parameters()).add_(.001)
    elif bad == "checksum":
        expected_sha = "c" * 64
    before = semantic_digest(capture_rng())
    if bad:
        with pytest.raises(ValueError):
            load_supplement_student(checkpoint, expected_sha=expected_sha, dataset=dataset, device="cpu", bundle=bundle)
    else:
        loaded = load_supplement_student(checkpoint, expected_sha=expected_sha, dataset=dataset, device="cpu", bundle=bundle)
        assert semantic_digest(loaded.state_dict()) == expected_network
        assert not any(parameter.requires_grad for parameter in loaded.parameters())
        assert not any(module.training for module in loaded.modules())
    assert semantic_digest(capture_rng()) == before
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == original_sha
