from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from audit_local_seed_history import audit_history, project_jobs
from native_runner.training.fair_final.storage import json_bytes


@pytest.mark.parametrize("damage", ["schema", "empty", "missing_seed", "bool", "float", "overflow"])
def test_reviewed_jobs_reject_ambiguous_or_invalid_seed_inputs(damage):
    document = dict(schema="historical-test", jobs=[dict(seed=100)])
    if damage == "schema":
        document["schema"] = "different"
    elif damage == "empty":
        document["jobs"] = []
    elif damage == "missing_seed":
        document["jobs"] = [dict(pair=0, seed_base=100)]
    else:
        document["jobs"][0]["seed"] = {"bool": True, "float": 100.0, "overflow": 1 << 32}[damage]
    with pytest.raises(ValueError):
        project_jobs(document, expected_schema="historical-test", seed_key="seed")


def test_reviewed_jobs_preserve_paired_seed_and_ignore_unverified_schedule():
    document = dict(schema="historical-test", jobs=[dict(seed=100), dict(seed=100)],
        evaluations=dict(screen=dict(seed_base=1000, pairs=200)))
    before = deepcopy(document)
    assert project_jobs(document, expected_schema="historical-test", seed_key="seed") == (
        [100, 100], ["/jobs/0/seed", "/jobs/1/seed"])
    assert document == before


def make_sources(root):
    sources = []
    for index, scope in enumerate(("training", "value_training", "evaluation")):
        relative = scope + "-source.json"
        (root / relative).write_bytes(json_bytes(dict(schema=scope, jobs=[dict(seed=100 + index)])))
        sources.append((scope, relative, scope, "seed"))
    return sources


def test_partial_audit_reentry_and_source_change_preserve_original_completion(tmp_path):
    sources = make_sources(tmp_path)
    output = tmp_path / "audit"
    report = audit_history(tmp_path, output, sources=sources)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert audit_history(tmp_path, output, sources=sources) == report
    assert report["unique_excluded_seeds"] == 3
    for field in ("training_admissible", "formal_g2_pass", "historical_census_complete",
            "historical_preregistration_proven", "collection_registration_created"):
        assert report[field] is False
    path = tmp_path / sources[0][1]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        audit_history(tmp_path, output, sources=sources)
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_changed_source_between_projection_and_verification_has_no_completion(tmp_path, monkeypatch):
    import audit_local_seed_history as audit_module

    sources = make_sources(tmp_path)
    original = audit_module.verify_seed_sources

    def change_source(*args, **kwargs):
        path = kwargs["source_paths"][0]
        path.write_bytes(path.read_bytes() + b"\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(audit_module, "verify_seed_sources", change_source)
    output = tmp_path / "audit"
    with pytest.raises(ValueError):
        audit_history(tmp_path, output, sources=sources)
    assert not (output / "audit.json").exists()
    assert not (output / "training.json").exists()


def test_cross_scope_overlap_is_reported_without_hiding_it(tmp_path):
    sources = make_sources(tmp_path)
    path = tmp_path / "evaluation-source.json"
    document = json.loads(path.read_bytes())
    document["jobs"][0]["seed"] = 100
    path.write_bytes(json_bytes(document))
    report = audit_history(tmp_path, tmp_path / "audit", sources=sources)
    assert report["cross_scope_overlaps"]["evaluation:training"] == [100]
    assert report["unique_excluded_seeds"] == 2
