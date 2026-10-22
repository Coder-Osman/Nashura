import importlib.util
import io
from pathlib import Path
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

from build_payload import build, SERIES, SELECTION, PROBE
from test_round_selection import CONFIG, initial_trainer
from test_series_training import WEIGHTS, series
from round_selection import MigrationRound
from native_runner.training.fair_final.checkpoint import capture_rng, restore_rng
from native_runner.training.fair_search.contracts import semantic_digest


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("deterministic", (False, True))
def test_cached_dataset_updates_validation_and_resume_match_reference(series, tmp_path, monkeypatch, deterministic):
    parent = ROOT.parent / "fair-final-migration-integration-v1/native-selected-round0-1792629813912401000"
    package, identity, changed = build(parent, deterministic=deterministic)
    expected_changes = {SERIES + "verified_store.py", SERIES + "series_training.py",
        SELECTION + "train_round.py", SELECTION + "round_selection.py"}
    if deterministic:
        expected_changes.update({SERIES + "deterministic_runtime.py", PROBE})
    assert set(changed) == expected_changes
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    cached_series = load_module("cached_series_training", tmp_path / SERIES / "series_training.py")
    with monkeypatch.context() as context:
        context.setitem(sys.modules, "series_training", cached_series)
        selection = load_module("cached_round_selection", tmp_path / SELECTION / "round_selection.py")
    cached = cached_series.SeriesMigrationDataset(series["initial"].plan,
        [store.root for store in series["initial"].stores], teacher=series["teacher"])
    assert cached.identity == series["initial"].identity
    assert cached.splits == series["initial"].splits
    for split, rows in cached.splits.items():
        actual, actual_legal = cached.batch(rows, split=split)
        expected, expected_legal = series["initial"].batch(rows, split=split)
        assert semantic_digest(actual) == semantic_digest(expected)
        assert semantic_digest([item.plays for item in actual_legal]) == semantic_digest([item.plays for item in expected_legal])
    reference = MigrationRound(initial_trainer(series), series["initial"], config=CONFIG)
    cached_config = selection.SelectionConfig(patience=CONFIG.patience, min_delta=CONFIG.min_delta)
    optimized = selection.MigrationRound(initial_trainer(series), cached, config=cached_config)
    assert reference.history == optimized.history
    state = capture_rng()
    expected = reference.advance(series["initial"])
    restore_rng(state)
    actual = optimized.advance(cached)
    assert expected == actual
    assert semantic_digest(reference.trainer.student.state_dict()) == semantic_digest(optimized.trainer.student.state_dict())
    assert semantic_digest(reference.trainer.optimizer.state_dict()) == semantic_digest(optimized.trainer.optimizer.state_dict())
    path = tmp_path / "midpoint.pt"
    digest = optimized.save(path)
    expected = optimized.advance(cached)
    final_rng = semantic_digest(capture_rng())
    resumed = selection.resume_round(path, expected_sha=digest, dataset=cached, weights=WEIGHTS,
        config=cached_config, bundle=series["bundle"])
    assert resumed.advance(cached) == expected
    assert semantic_digest(capture_rng()) == final_rng
    assert semantic_digest(optimized.trainer.student.state_dict()) == semantic_digest(resumed.trainer.student.state_dict())
    assert semantic_digest(optimized.trainer.optimizer.state_dict()) == semantic_digest(resumed.trainer.optimizer.state_dict())
    assert optimized.history == resumed.history
    assert optimized.trainer.batch_ids() == resumed.trainer.batch_ids()
    with pytest.raises(ValueError, match="split"):
        cached.batch(cached.splits["validation"], split="train")
