import hashlib
from pathlib import Path
import sys

import pytest
import torch

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1")]

from test_batched_migration import optimized, payload
from test_round_selection import CONFIG, initial_trainer
from test_series_training import WEIGHTS, series
import round_selection
import series_training
from native_runner.training import fair_migration_trainer
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_search.contracts import semantic_digest


@pytest.mark.parametrize("updates", [0, 1, 3, 5])
def test_batched_exact_resume_and_implementation_isolation(series, optimized, tmp_path, monkeypatch, updates):
    legacy = round_selection.MigrationRound(initial_trainer(series), series["initial"], config=CONFIG)
    legacy_path = tmp_path / "legacy.pt"
    legacy_sha = legacy.save(legacy_path)
    original_identity = series_training.implementation_sha()
    monkeypatch.setattr(series_training, "implementation_sha", lambda: semantic_digest(dict(
        parent=original_identity, batched=hashlib.sha256((ROOT / "batched_migration.py").read_bytes()).hexdigest())))
    monkeypatch.setattr(fair_migration_trainer, "MigrationLossWeights", optimized.MigrationLossWeights)
    monkeypatch.setattr(fair_migration_trainer, "migration_batch_loss", optimized.migration_batch_loss)
    monkeypatch.setattr(round_selection, "migration_batch_loss", optimized.migration_batch_loss)
    weights = optimized.MigrationLossWeights(**vars(WEIGHTS))
    options = dict(dataset=series["initial"], weights=weights, config=CONFIG, bundle=series["bundle"])
    with pytest.raises(ValueError, match="implementation"):
        round_selection.resume_round(legacy_path, expected_sha=legacy_sha, **options)
    torch.manual_seed(11841)
    from native_runner.training.fair_final.model import ConditionalPolicyValue
    student = ConditionalPolicyValue(bundle=series["bundle"],
        encoder_config=series["teacher"].policy.backbone.config)
    trainer = fair_migration_trainer.MigrationTrainer(series["teacher"], student,
        identity=series["initial"].identity, splits=series["initial"].splits,
        weights=weights, seed=822, batch_size=3)
    run = round_selection.MigrationRound(trainer, series["initial"], config=CONFIG)
    for _ in range(updates):
        run.advance(series["initial"])
    path = tmp_path / "batched.pt"
    digest = run.save(path)
    expected_rng = semantic_digest(capture_rng())
    expected_rows = run.trainer.batch_ids()
    expected_metrics = run.advance(series["initial"])
    expected = semantic_digest(dict(network=run.trainer.student.state_dict(),
        optimizer=run.trainer.optimizer.state_dict(), history=run.history, best=run.best, rng=capture_rng()))
    torch.manual_seed(9912)
    resumed = round_selection.resume_round(path, expected_sha=digest, **options)
    assert semantic_digest(capture_rng()) == expected_rng
    assert resumed.trainer.batch_ids() == expected_rows
    assert resumed.advance(series["initial"]) == expected_metrics
    assert semantic_digest(dict(network=resumed.trainer.student.state_dict(),
        optimizer=resumed.trainer.optimizer.state_dict(), history=resumed.history,
        best=resumed.best, rng=capture_rng())) == expected
