from copy import copy, deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

import pytest
import torch

import round_selection
from round_selection import (MigrationRound, SelectionConfig, evaluate_validation, load_selected_student,
    resume_round, selected_parent)
from test_series_training import WEIGHTS, add_round, series
from series_training import SeriesMigrationDataset
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_migration_trainer import MigrationTrainer
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.storage import torch_bytes


CONFIG = SelectionConfig(patience=1, min_delta=1e6)


def initial_trainer(series):
    torch.manual_seed(11841)
    student = ConditionalPolicyValue(bundle=series["bundle"],
        encoder_config=series["teacher"].policy.backbone.config)
    dataset = series["initial"]
    return MigrationTrainer(series["teacher"], student, identity=dataset.identity,
        splits=dataset.splits, weights=WEIGHTS, seed=822, batch_size=3)


def create(series, config=CONFIG):
    return MigrationRound(initial_trainer(series), series["initial"], config=config)


def restore(path, digest, series, config=CONFIG):
    return resume_round(path, expected_sha=digest, dataset=series["initial"], weights=WEIGHTS,
        config=config, bundle=series["bundle"])


def finish(run, dataset):
    while run.terminal is None:
        run.advance(dataset)


def test_validation_is_read_only_rng_and_module_mode_preserving_and_never_reads_test(series, monkeypatch):
    trainer = initial_trainer(series)
    dataset = series["initial"]
    modes = [(module, module.training) for model in (trainer.student, trainer.teacher) for module in model.modules()]
    before = semantic_digest(dict(rng=capture_rng(), student=trainer.student.state_dict(),
        teacher=trainer.teacher.state_dict(), optimizer=trainer.optimizer.state_dict()))
    seen = []
    original = dataset.batch

    def batch(rows, *, split="train"):
        seen.extend((row, split) for row in rows)
        return original(rows, split=split)

    monkeypatch.setattr(dataset, "batch", batch)
    result = evaluate_validation(trainer, dataset)
    assert result["rows"] == len(dataset.splits["validation"])
    assert seen == [(row, "validation") for row in dataset.splits["validation"]]
    assert before == semantic_digest(dict(rng=capture_rng(), student=trainer.student.state_dict(),
        teacher=trainer.teacher.state_dict(), optimizer=trainer.optimizer.state_dict()))
    assert all(module.training == mode for module, mode in modes)


def test_validation_weights_games_equally_even_with_unequal_lengths(series, monkeypatch):
    trainer = initial_trainer(series)
    original = series["initial"]
    dataset = copy(original)
    dataset.splits = deepcopy(original.splits)
    train = original.splits["train"]
    validation = original.splits["validation"]
    dataset.splits["validation"] = train[:1] + validation
    trainer.splits = deepcopy(dataset.splits)
    dataset.index = dict(original.index)
    for row in train[:1]:
        number, match, offset, _ = dataset.index[row]
        dataset.index[row] = (number, match, offset, "validation")
    values = {row: float(index + 1) for index, row in enumerate(dataset.splits["validation"])}
    current = []

    def batch(rows, *, split):
        current[:] = rows
        return original.batch(rows, split="train" if rows[0] in train else "validation")

    def loss(*args, **kwargs):
        value = values[current[0]]
        return {key: torch.tensor(value) for key in round_selection.METRICS}

    monkeypatch.setattr(dataset, "batch", batch)
    monkeypatch.setattr(round_selection, "migration_batch_loss", loss)
    metrics = evaluate_validation(trainer, dataset)
    expected = (1. + sum(values[row] for row in validation) / len(validation)) / 2
    assert metrics["loss"] == expected
    assert metrics["loss"] != sum(values.values()) / len(values)


def test_early_stop_runs_frozen_epochs_and_retains_best_optimizer_instead_of_latest(series):
    run = create(series)
    best = semantic_digest(run.best)
    finish(run, series["initial"])
    assert run.terminal == "EARLY_STOP"
    assert run.trainer.completed_epochs == 3
    assert [item["epoch"] for item in run.history] == [0, 1, 2, 3]
    assert run.trainer.stage == "TRAINABLE_ENCODER"
    assert run.best_index == 0 and semantic_digest(run.best) == best
    assert run.best["payload"]["cursor"]["optimizer_steps"] == 0
    assert run.trainer.optimizer_steps == 6 and run.trainer.optimizer.state
    assert not run.best["payload"]["optimizer"]["state"]
    with pytest.raises(ValueError, match="terminal"):
        run.advance(series["initial"])


@pytest.mark.parametrize("updates", [0, 1, 2, 4, 5, 6])
def test_exact_resume_restores_selection_model_optimizer_rng_and_next_batch(series, tmp_path, updates):
    run = create(series)
    for _ in range(updates):
        run.advance(series["initial"])
    path = tmp_path / "round.pt"
    digest = run.save(path)
    rng = semantic_digest(capture_rng())
    next_ids = run.trainer.batch_ids()
    expected = run.advance(series["initial"]) if run.terminal is None else None
    state = semantic_digest(dict(network=run.trainer.student.state_dict(), optimizer=run.trainer.optimizer.state_dict(),
        history=run.history, best=run.best, rng=capture_rng()))
    torch.manual_seed(9123)
    resumed = restore(path, digest, series)
    assert semantic_digest(capture_rng()) == rng
    assert resumed.trainer.batch_ids() == next_ids
    actual = resumed.advance(series["initial"]) if resumed.terminal is None else None
    assert actual == expected
    assert semantic_digest(dict(network=resumed.trainer.student.state_dict(), optimizer=resumed.trainer.optimizer.state_dict(),
        history=resumed.history, best=resumed.best, rng=capture_rng())) == state


def test_selected_parent_supports_two_supplement_rounds_and_uses_best_moments(series, tmp_path):
    first = create(series)
    finish(first, series["initial"])
    path = tmp_path / "initial.pt"
    digest = first.save(path)
    plan = deepcopy(series["plan"])
    plan["rounds"] = plan["rounds"][:1]
    roots = list(series["roots"][:1])
    add_round(plan, roots, series["teacher"], tmp_path / "round1", parent_sha=digest)
    combined = SeriesMigrationDataset(plan, roots, teacher=series["teacher"])
    child = selected_parent(path, expected_sha=digest, parent_dataset=series["initial"], dataset=combined,
        weights=WEIGHTS, config=CONFIG, seed=482, bundle=series["bundle"], batch_size=5)
    assert child.optimizer_steps == 0
    assert semantic_digest(child.student.state_dict()) == first.history[0]["network_sha"]
    assert not child.optimizer.state
    assert semantic_digest(child.student.state_dict()) != semantic_digest(first.trainer.student.state_dict())
    config = SelectionConfig(patience=6, min_delta=0.)
    second = MigrationRound(child, combined, config=config)
    finish(second, combined)
    assert second.terminal == "MAX_EPOCHS" and child.completed_epochs == 5
    assert second.best_index > 0
    assert second.best["payload"]["optimizer"]["state"]
    path = tmp_path / "round1.pt"
    digest = second.save(path)
    add_round(plan, roots, series["teacher"], tmp_path / "round2", parent_sha=digest)
    final = SeriesMigrationDataset(plan, roots, teacher=series["teacher"])
    next_trainer = selected_parent(path, expected_sha=digest, parent_dataset=combined, dataset=final,
        weights=WEIGHTS, config=config, seed=483, bundle=series["bundle"], batch_size=7)
    assert next_trainer.optimizer_steps == second.history[second.best_index]["optimizer_steps"]
    assert semantic_digest(next_trainer.optimizer.state_dict()) == semantic_digest(second.best["payload"]["optimizer"])
    assert semantic_digest(next_trainer.student.state_dict()) == second.history[second.best_index]["network_sha"]
    third = MigrationRound(next_trainer, final, config=SelectionConfig(patience=1, min_delta=1e6))
    third.advance(final)
    target = tmp_path / "round2.pt"
    sha = third.save(target)
    resumed = resume_round(target, expected_sha=sha, dataset=final, weights=WEIGHTS,
        config=third.config, bundle=series["bundle"])
    assert third.trainer.batch_ids() == resumed.trainer.batch_ids()
    assert third.advance(final) == resumed.advance(final)
    assert semantic_digest(third.trainer.student.state_dict()) == semantic_digest(resumed.trainer.student.state_dict())


@pytest.mark.parametrize("bad", ["config", "implementation", "validation_identity", "best_index", "terminal",
    "missing_epoch", "steps", "network_sha", "game_rows", "game_id", "whole_game_weight",
    "loss_weights", "nonfinite", "best_network", "best_optimizer", "best_run_seed"])
def test_resealed_corruption_is_rejected_and_rng_restored(series, tmp_path, bad):
    run = create(series)
    finish(run, series["initial"])
    path = tmp_path / "round.pt"
    run.save(path)
    envelope = torch.load(path, weights_only=True)
    payload = envelope["payload"]
    if bad == "config":
        payload["config"]["patience"] += 1
    elif bad == "implementation":
        payload["implementation_sha"] = "f" * 64
    elif bad == "validation_identity":
        payload["validation_sha"] = "f" * 64
    elif bad == "best_index":
        payload["best_index"] = 1
    elif bad == "terminal":
        payload["terminal"] = None
    elif bad == "missing_epoch":
        payload["history"].pop()
    elif bad == "steps":
        payload["history"][1]["optimizer_steps"] += 1
    elif bad == "network_sha":
        payload["history"][-1]["network_sha"] = "f" * 64
    elif bad in ("game_rows", "game_id", "whole_game_weight", "loss_weights", "nonfinite"):
        metrics = payload["history"][0]["validation"]
        if bad == "game_rows":
            metrics["games"][0]["rows"] += 1
        elif bad == "game_id":
            metrics["games"][0]["match_id"] = "f" * 64
        elif bad == "whole_game_weight":
            metrics["loss"] += .1
        elif bad == "loss_weights":
            metrics["games"][0]["loss"] += .1
        else:
            metrics["games"][0]["greedy_nll"] = float("nan")
    else:
        inner = payload["best"]
        if bad == "best_network":
            next(iter(inner["payload"]["network"].values())).add_(1.)
        elif bad == "best_optimizer":
            inner["payload"]["optimizer"]["param_groups"][0]["lr"] = .01
        else:
            inner["payload"]["cursor"]["seed"] += 1
        inner["sha256"] = semantic_digest(inner["payload"])
    if bad != "nonfinite":
        envelope["sha256"] = semantic_digest(payload)
    data = torch_bytes(envelope)
    path.write_bytes(data)
    before = semantic_digest(capture_rng())
    with pytest.raises((ValueError, RuntimeError)):
        restore(path, hashlib.sha256(data).hexdigest(), series)
    assert semantic_digest(capture_rng()) == before


def test_validation_failure_restores_modes_and_rng_and_quarantines_round(series, monkeypatch, tmp_path):
    run = create(series)
    dataset = series["initial"]
    run.advance(dataset)
    before_rng = semantic_digest(capture_rng())

    def broken(*args, **kwargs):
        torch.rand(3)
        raise ValueError("validation transport failed")

    monkeypatch.setattr(round_selection, "migration_batch_loss", broken)
    with pytest.raises(ValueError, match="validation transport"):
        run.advance(dataset)
    assert semantic_digest(capture_rng()) == before_rng
    assert run.state == "FAILED"
    assert run.trainer.student.training
    assert not any(module.training for module in run.trainer.teacher.modules())
    with pytest.raises(ValueError, match="quarantined"):
        run.save(tmp_path / "invalid.pt")


def test_export_preserves_selected_probabilities_and_caller_rng(series, tmp_path):
    run = create(series)
    finish(run, series["initial"])
    path = tmp_path / "round.pt"
    digest = run.save(path)
    before = semantic_digest(capture_rng())
    model = load_selected_student(path, expected_sha=digest, dataset=series["initial"], weights=WEIGHTS,
        config=CONFIG, bundle=series["bundle"])
    assert semantic_digest(capture_rng()) == before
    assert not any(module.training for module in model.modules())
    assert not any(parameter.requires_grad for parameter in model.parameters())
    assert semantic_digest(model.state_dict()) == run.history[run.best_index]["network_sha"]
    reference = initial_trainer(series).student.eval()
    frame, legalities = series["initial"].batch(series["initial"].splits["validation"][:1], split="validation")
    with torch.no_grad():
        expected, expected_wdl = reference(frame, legalities)
        actual, actual_wdl = model(frame, legalities)
        generator = torch.Generator().manual_seed(17)
        action = expected[0].sample(generator, greedy=True)[0]
        assert torch.equal(expected_wdl, actual_wdl)
        assert torch.equal(expected[0].log_prob(action), actual[0].log_prob(action))


@pytest.mark.parametrize("patience,delta", [(0, 0.), (True, 0.), (1, -1.), (1, math.inf), (1, math.nan)])
def test_invalid_early_stop_configuration_rejected(patience, delta):
    with pytest.raises(ValueError):
        SelectionConfig(patience, delta)


@pytest.mark.parametrize("bad", ["unfinished", "prefix", "receipt"])
def test_parent_handoff_rejects_incomplete_or_mismatched_history(series, tmp_path, bad):
    run = create(series)
    if bad != "unfinished":
        finish(run, series["initial"])
    path = tmp_path / "parent.pt"
    digest = run.save(path)
    plan = deepcopy(series["plan"])
    plan["rounds"] = plan["rounds"][:1]
    roots = list(series["roots"][:1])
    add_round(plan, roots, series["teacher"], tmp_path / "supplement", parent_sha=digest)
    dataset = SeriesMigrationDataset(plan, roots, teacher=series["teacher"])
    if bad == "prefix":
        dataset.stores = [copy(store) for store in dataset.stores]
        dataset.stores[0].dataset_sha = "f" * 64
    elif bad == "receipt":
        dataset.receipts = dict(dataset.receipts)
        dataset.receipts[next(iter(series["initial"].receipts))] = "f" * 64
    before = semantic_digest(capture_rng())
    with pytest.raises(ValueError, match="terminal|prefix"):
        selected_parent(path, expected_sha=digest, parent_dataset=series["initial"], dataset=dataset,
            weights=WEIGHTS, config=CONFIG, seed=283, bundle=series["bundle"])
    assert semantic_digest(capture_rng()) == before
    if bad == "unfinished":
        with pytest.raises(ValueError, match="terminal"):
            load_selected_student(path, expected_sha=digest, dataset=series["initial"],
                weights=WEIGHTS, config=CONFIG, bundle=series["bundle"])
        assert semantic_digest(capture_rng()) == before


def test_repeated_save_requires_correct_previous_sha_and_preserves_file_on_conflict(series, tmp_path):
    run = create(series)
    path = tmp_path / "round.pt"
    original = run.save(path)
    run.advance(series["initial"])
    with pytest.raises(FileExistsError):
        run.save(path)
    with pytest.raises(ValueError, match="previous artifact checksum"):
        run.save(path, expected_previous_sha="f" * 64)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    updated = run.save(path, expected_previous_sha=original)
    assert updated != original
    assert restore(path, updated, series).trainer.optimizer_steps == 1


def test_engineering_entrypoint_updates_and_resumes_without_old_stream_logs(series, tmp_path, monkeypatch, capsys):
    import train_round

    plan = dict(series["plan"], rounds=series["plan"]["rounds"][:1])
    files = {"plan": plan, "config": vars(CONFIG), "weights": vars(WEIGHTS)}
    for name, value in files.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    output = tmp_path / "round.pt"
    argv = ["train_round.py", "--engineering", "--source", str(tmp_path / "source.pt"),
        "--private-plan", str(tmp_path / "plan.json"), "--data-roots", str(series["roots"][0]),
        "--selection-config", str(tmp_path / "config.json"),
        "--loss-weights", str(tmp_path / "weights.json"), "--output", str(output),
        "--seed", "822", "--batch-size", "3", "--max-updates", "1"]

    def source(path, *, device, expected_sha256):
        assert expected_sha256 == plan["rounds"][0]["manifest"]["identity"]["source_a_sha"]
        return series["teacher"], None, None

    monkeypatch.setattr(train_round, "load_checkpoint", source)
    monkeypatch.setattr(train_round, "ConditionalPolicyValue", lambda **kwargs:
        ConditionalPolicyValue(bundle=series["bundle"], **kwargs))
    original_resume = train_round.resume_round
    monkeypatch.setattr(train_round, "resume_round", lambda *args, **kwargs:
        original_resume(*args, bundle=series["bundle"], **kwargs))
    monkeypatch.setattr(sys, "argv", argv)
    train_round.main()
    first_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    first = json.loads(output.with_suffix(".report.json").read_text())
    assert first["latest_optimizer_steps"] == 1 and first["terminal"] is None
    assert first["training_admissible"] is False and first["formal_g2_pass"] is False
    expected = restore(output, first_sha, series)
    expected.advance(series["initial"])
    monkeypatch.setattr(sys, "argv", argv + ["--resume-sha", first_sha])
    train_round.main()
    second_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    actual = restore(output, second_sha, series)
    assert actual.trainer.optimizer_steps == 2
    assert semantic_digest(actual.trainer.student.state_dict()) == semantic_digest(expected.trainer.student.state_dict())
    assert semantic_digest(actual.trainer.optimizer.state_dict()) == semantic_digest(expected.trainer.optimizer.state_dict())
    assert actual.history == expected.history
    for name, value in (("--seed", "999"), ("--batch-size", "4"), ("--micro-batch-size", "2")):
        monkeypatch.setattr(sys, "argv", argv + ["--resume-sha", second_sha, name, value])
        with pytest.raises(SystemExit) as error:
            train_round.main()
        assert error.value.code == 2
        assert hashlib.sha256(output.read_bytes()).hexdigest() == second_sha
    capsys.readouterr()
