import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / name) for name in (
    "fair-final-migration-selection-v1", "fair-final-migration-series-v1",
    "fair-final-migration-integration-v1")]
sys.path.insert(0, str(ROOT.parents[2] / "firstlight_CR"))

import round_selection
from probe_native_resume import round_state
from test_round_selection import create
from test_series_training import WEIGHTS, series


spec = importlib.util.spec_from_file_location("active_resume_probe", ROOT / "probe_active_resume.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
CONFIG = round_selection.SelectionConfig(patience=2, min_delta=1e6)


@pytest.mark.parametrize("stage,updates", (("frozen", 1), ("trainable", 5)))
def test_resume_probe_checks_real_optimizer_selection_rng_and_rejects_changed_metrics(
        series, tmp_path, monkeypatch, stage, updates):
    run = create(series, config=CONFIG)
    for _ in range(updates):
        run.advance(series["initial"])
    midpoint_sha = run.save(tmp_path / (stage + "-midpoint.pt"))
    midpoint = round_state(run)
    metrics = probe.advance(run, series["initial"], 2)
    expected = round_state(run)
    reference = dict(branches={stage: dict(midpoint_sha256=midpoint_sha,
        midpoint_state=midpoint, metrics=metrics, expected=expected)})
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(reference))
    original = round_selection.resume_round

    def restore(checkpoint, **kwargs):
        kwargs.update(device="cpu", bundle=series["bundle"])
        return original(checkpoint, **kwargs)

    monkeypatch.setattr(round_selection, "resume_round", restore)
    report = probe.resumed(None, series["initial"], WEIGHTS, CONFIG, tmp_path, stage, probe.digest(path))
    assert report["exact_model_optimizer_rng_sampler_selection"] is True
    assert report["actual"] == expected
    with pytest.raises(ValueError, match="pinned reference"):
        probe.resumed(None, series["initial"], WEIGHTS, CONFIG, tmp_path, stage, "0" * 64)
    reference["branches"][stage]["metrics"][0]["loss"] += 1
    path.write_text(json.dumps(reference))
    with pytest.raises(ValueError, match="resumed model/optimizer"):
        probe.resumed(None, series["initial"], WEIGHTS, CONFIG, tmp_path, stage, probe.digest(path))
