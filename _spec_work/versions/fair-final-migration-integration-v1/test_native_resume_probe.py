import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

from probe_native_resume import reference_branch, resumed_branch
from verify_native_resume import sha, verify_comparison
from round_selection import resume_round
from test_round_selection import CONFIG, create
from test_series_training import WEIGHTS, series


def test_native_resume_probe_compares_actual_gradient_and_sampler_state(series, tmp_path):
    run = create(series)
    reference = reference_branch(run, series["initial"], tmp_path)
    restored = resume_round(tmp_path / "midpoint.pt", expected_sha=reference["midpoint_sha256"],
        dataset=series["initial"], weights=WEIGHTS, config=CONFIG, bundle=series["bundle"])
    result = resumed_branch(restored, series["initial"], reference, tmp_path)
    assert result["exact_model_optimizer_rng_sampler_selection"] is True
    assert result["actual"]["steps"] == 2
    assert result["training_admissible"] is False
    (tmp_path / "reference.json").write_text(json.dumps(reference))
    (tmp_path / "resumed.json").write_text(json.dumps(result))
    report = verify_comparison(tmp_path, reference_sha=sha(tmp_path / "reference.json"),
        resumed_sha=sha(tmp_path / "resumed.json"))
    assert report["steps"] == 2
    assert report["native_deployment_verified"] is False
    result["actual"]["optimizer"] = "0" * 64
    (tmp_path / "resumed.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="checkpoint content"):
        verify_comparison(tmp_path, reference_sha=sha(tmp_path / "reference.json"),
            resumed_sha=sha(tmp_path / "resumed.json"))


def test_native_resume_probe_rejects_changed_reference_before_update(series, tmp_path):
    run = create(series)
    reference = reference_branch(run, series["initial"], tmp_path)
    restored = resume_round(tmp_path / "midpoint.pt", expected_sha=reference["midpoint_sha256"],
        dataset=series["initial"], weights=WEIGHTS, config=CONFIG, bundle=series["bundle"])
    reference["midpoint_state"]["optimizer"] = "0" * 64
    with pytest.raises(ValueError, match="midpoint state differs"):
        resumed_branch(restored, series["initial"], reference, tmp_path)
    assert restored.trainer.optimizer_steps == 1
