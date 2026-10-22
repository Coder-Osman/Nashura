from copy import deepcopy
import hashlib
import io

import pytest
import torch

from native_runner.tests.test_fair_final_learning import fixture_source
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.fair_final.checkpoint import capture_rng, restore_rng
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import LegalPrefixes
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_trainer import MigrationTrainer, resume_migration
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.storage import torch_bytes


WEIGHTS = MigrationLossWeights(1., .1, 1.)
IDENTITY = {key: semantic_digest(key) for key in ("source_a_sha", "migration_adapter_sha", "dataset_sha")}
SPLITS = {name: [semantic_digest((name, row)) for row in range(count)]
    for name, count in (("train", 3), ("validation", 1), ("test", 1))}


def setup_trainer(*, micro_batch_size=1):
    torch.manual_seed(713)
    teacher, bundle = fixture_source()
    teacher.requires_grad_(False)
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    student.copy_encoder(teacher)
    trainer = MigrationTrainer(teacher, student, identity=IDENTITY, splits=SPLITS,
        weights=WEIGHTS, seed=927, batch_size=2, micro_batch_size=micro_batch_size)
    return trainer, bundle


def advance(trainer):
    ids = trainer.batch_ids()
    frame = _batch(len(ids))
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    plays[0, 0, 1:3] = True
    plays[1, 1, 2:4] = True
    legalities = [LegalPrefixes(trainer.student.encoder.catalog.raw_card_ids, plays) for row in ids]
    return trainer.update(frame, legalities, row_ids=ids)


def restore(path, sha, trainer, bundle, **changes):
    arguments = dict(expected_sha=sha, teacher=trainer.teacher, identity=IDENTITY,
        splits=SPLITS, weights=WEIGHTS, bundle=bundle)
    arguments.update(changes)
    return resume_migration(path, **arguments)


def test_real_migration_stages_bound_epochs_and_complete_row_coverage():
    trainer, bundle = setup_trainer()
    teacher_sha = semantic_digest(trainer.teacher.state_dict())
    encoder_sha = semantic_digest(trainer.student.encoder.state_dict())
    seen, stages = [], []
    while trainer.batch_ids():
        seen.extend(trainer.batch_ids())
        result = advance(trainer)
        stages.append(result["stage"])
        assert result["rows"] in (1, 2)
        assert all(parameter.grad is None for parameter in trainer.student.parameters())
        if len(stages) <= 4:
            assert semantic_digest(trainer.student.encoder.state_dict()) == encoder_sha
    assert stages[:4] == ["FROZEN_ENCODER"] * 4
    assert stages[4:] == ["TRAINABLE_ENCODER"] * 20
    assert trainer.stage == "COMPLETE" and trainer.completed_epochs == 12
    assert trainer.optimizer_steps == 24 and trainer.consumed == 36
    assert all(seen.count(row) == 12 for row in SPLITS["train"])
    assert not set(seen) & set(SPLITS["validation"] + SPLITS["test"])
    assert semantic_digest(trainer.student.encoder.state_dict()) != encoder_sha
    assert semantic_digest(trainer.teacher.state_dict()) == teacher_sha


@pytest.mark.parametrize("updates", [0, 1, 3, 4, 5, 24])
def test_migration_resume_exact_across_partial_batch_and_encoder_boundary(tmp_path, updates):
    trainer, bundle = setup_trainer()
    for ordinal in range(updates):
        advance(trainer)
    path = tmp_path / "migration.pt"
    digest = trainer.save(path)
    before = capture_rng()
    expected_rows = trainer.batch_ids()
    expected = advance(trainer) if expected_rows else None
    expected_network = semantic_digest(trainer.student.state_dict())
    expected_optimizer = semantic_digest(trainer.optimizer.state_dict())
    expected_rng = semantic_digest(capture_rng())
    torch.manual_seed(98743)
    restored = restore(path, digest, trainer, bundle)
    assert semantic_digest(capture_rng()) == semantic_digest(before)
    assert restored.batch_ids() == expected_rows
    actual = advance(restored) if expected_rows else None
    assert actual == expected
    assert semantic_digest(restored.student.state_dict()) == expected_network
    assert semantic_digest(restored.optimizer.state_dict()) == expected_optimizer
    assert semantic_digest(capture_rng()) == expected_rng
    assert (restored.offset, restored.completed_epochs, restored.consumed) == (
        trainer.offset, trainer.completed_epochs, trainer.consumed)


@pytest.mark.parametrize("mutation", ["source", "adapter", "dataset", "split", "weight", "file", "teacher"])
def test_migration_resume_rejects_foreign_identity_without_consuming_rng(tmp_path, mutation):
    trainer, bundle = setup_trainer()
    advance(trainer)
    path = tmp_path / "migration.pt"
    digest = trainer.save(path)
    changes = {}
    identity = deepcopy(IDENTITY)
    if mutation in ("source", "adapter", "dataset"):
        key = {"source": "source_a_sha", "adapter": "migration_adapter_sha", "dataset": "dataset_sha"}[mutation]
        identity[key] = semantic_digest("foreign")
        changes["identity"] = identity
    elif mutation == "split":
        changes["splits"] = {**SPLITS, "test": SPLITS["validation"], "validation": SPLITS["test"]}
    elif mutation == "weight":
        changes["weights"] = MigrationLossWeights(1., 1., 1.)
    elif mutation == "file":
        changes["expected_sha"] = semantic_digest("wrong_file")
    else:
        with torch.no_grad():
            next(trainer.teacher.parameters()).add_(.1)
    before = semantic_digest(capture_rng())
    with pytest.raises(ValueError):
        restore(path, digest, trainer, bundle, **changes)
    assert semantic_digest(capture_rng()) == before


@pytest.mark.parametrize("mutation", ["order", "cursor_rng", "consumed", "epoch", "steps", "schedule",
    "optimizer", "moment", "dtype", "architecture", "nonfinite", "semantic"])
def test_migration_resume_rejects_corrupt_state_even_with_resealed_file(tmp_path, mutation):
    trainer, bundle = setup_trainer()
    advance(trainer)
    path = tmp_path / "original.pt"
    trainer.save(path)
    envelope = torch.load(io.BytesIO(path.read_bytes()), weights_only=True)
    payload = envelope["payload"]
    if mutation == "order": payload["cursor"]["order"].reverse()
    elif mutation == "cursor_rng": payload["cursor"]["rng"] = capture_rng()["python"]
    elif mutation == "consumed": payload["cursor"]["consumed"] += 1
    elif mutation == "epoch": payload["cursor"]["completed_epochs"] = 2
    elif mutation == "steps": payload["cursor"]["optimizer_steps"] += 1
    elif mutation == "schedule": payload["cursor"]["frozen_epochs"] = 0
    elif mutation == "optimizer": payload["optimizer"]["param_groups"][0]["lr"] = .1
    elif mutation == "moment":
        next(iter(payload["optimizer"]["state"].values()))["exp_avg_sq"].fill_(-1)
    elif mutation == "dtype":
        key = next(iter(payload["network"]))
        payload["network"][key] = payload["network"][key].double()
    elif mutation == "architecture": payload["architecture"]["value_order"].reverse()
    elif mutation == "nonfinite":
        key = next(key for key, value in payload["network"].items() if value.is_floating_point())
        payload["network"][key].fill_(torch.nan)
    else: envelope["sha256"] = semantic_digest("wrong_semantic")
    if mutation not in ("semantic", "nonfinite"):
        envelope["sha256"] = semantic_digest(payload)
    corrupt = tmp_path / "corrupt.pt"
    data = torch_bytes(envelope)
    corrupt.write_bytes(data)
    before = semantic_digest(capture_rng())
    with pytest.raises((ValueError, TypeError)):
        restore(corrupt, hashlib.sha256(data).hexdigest(), trainer, bundle)
    assert semantic_digest(capture_rng()) == before


def test_migration_update_rejects_validation_rows_before_any_update():
    trainer, bundle = setup_trainer()
    before = semantic_digest(trainer.student.state_dict())
    with pytest.raises(ValueError, match="cursor"):
        trainer.update(_batch(2), [], row_ids=SPLITS["validation"] + SPLITS["test"])
    assert trainer.consumed == 0 and trainer.optimizer_steps == 0
    assert semantic_digest(trainer.student.state_dict()) == before
    assert trainer.state == "RUNNING"


def test_migration_failure_quarantines_updates_and_checkpoint(tmp_path):
    trainer, bundle = setup_trainer()
    with pytest.raises(ValueError):
        trainer.update(_batch(2), [], row_ids=trainer.batch_ids())
    assert trainer.state == "FAILED" and trainer.consumed == 0
    assert all(parameter.grad is None for parameter in trainer.student.parameters())
    with pytest.raises(ValueError, match="quarantined"):
        trainer.batch_ids()
    with pytest.raises(ValueError, match="failed migration"):
        trainer.save(tmp_path / "failed.pt")
    assert not (tmp_path / "failed.pt").exists()


def test_migration_rejects_overlapping_frozen_splits():
    trainer, bundle = setup_trainer()
    with pytest.raises(ValueError, match="overlap"):
        MigrationTrainer(trainer.teacher, trainer.student, identity=IDENTITY,
            splits={**SPLITS, "validation": [SPLITS["train"][0]]}, weights=WEIGHTS, seed=1)


@pytest.mark.parametrize("updates", [0, 4])
def test_migration_micro_batches_match_full_batch_loss_and_parameter_update(updates):
    trainer, bundle = setup_trainer(micro_batch_size=2)
    for ordinal in range(updates):
        advance(trainer)
    streamed, bundle = setup_trainer(micro_batch_size=1)
    for ordinal in range(updates):
        advance(streamed)
    streamed.student.load_state_dict(trainer.student.state_dict())
    streamed.optimizer.load_state_dict(deepcopy(trainer.optimizer.state_dict()))
    gradients = {}
    def capture_gradients(name, model):
        def hook(optimizer, arguments, keywords):
            gradients[name] = {key: None if parameter.grad is None else parameter.grad.detach().clone()
                for key, parameter in model.named_parameters()}
        return hook
    full_hook = trainer.optimizer.register_step_pre_hook(capture_gradients("full", trainer.student))
    partial_hook = streamed.optimizer.register_step_pre_hook(capture_gradients("partial", streamed.student))
    before_rng = capture_rng()
    full = advance(trainer)
    restore_rng(before_rng)
    partial = advance(streamed)
    full_hook.remove()
    partial_hook.remove()
    for key in ("loss", "conditional_policy_kl", "greedy_nll", "wdl_teacher_kl", "gradient_norm"):
        assert partial[key] == pytest.approx(full[key], abs=2e-6, rel=2e-5)
    for key, value in gradients["full"].items():
        if value is None:
            assert gradients["partial"][key] is None
        else:
            torch.testing.assert_close(gradients["partial"][key], value, atol=1e-7, rtol=2e-5)
    for key, value in trainer.student.state_dict().items():
        torch.testing.assert_close(streamed.student.state_dict()[key], value, atol=1e-5, rtol=2e-5)
