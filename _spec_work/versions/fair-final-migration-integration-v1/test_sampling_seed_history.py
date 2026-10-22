from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-series-v1"),
    str(WORKSPACE / "firstlight_CR")]

from audit_sampling_seed_history import extend_inventory, review_sampling


EVIDENCE = WORKSPACE / "_spec_work/evidence"
SAMPLING = EVIDENCE / "teacher-sampling-eval-20261013"
BASE = EVIDENCE / "fair-final-v1-20261019/historical-seed-inventory-remote-final-20261022"
CODE = WORKSPACE / "firstlight_CR/native_runner/training/fair_search/evaluate_teacher.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_sampling_results_bind_all_pairs_and_explicit_seed_pointers():
    report, seeds = review_sampling(SAMPLING, CODE,
        status_sha=digest(SAMPLING / "status-latest.json"), code_sha=digest(CODE))
    assert report["result_rows"] == 256
    assert seeds == set(range(28614000, 28614064))
    assert len(report["source_projection"]["engine_seed_pointers"]) == 256


def test_real_inventory_extension_preserves_previous_scopes_and_reentry(tmp_path):
    inputs = dict(base_audit_sha=digest(BASE / "audit.json"), sampling_directory=SAMPLING,
        code_path=CODE, status_sha=digest(SAMPLING / "status-latest.json"), code_sha=digest(CODE))
    report = extend_inventory(BASE, EVIDENCE, tmp_path, **inputs)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert extend_inventory(BASE, EVIDENCE, tmp_path, **inputs) == report
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert report["unique_excluded_seeds"] == 5360
    assert report["unique_seeds_by_scope"] == dict(training=1136, value_training=4096, evaluation=128)
    assert report["added_evaluation_seeds"] == 64
    for scope in ("training", "value_training"):
        assert (tmp_path / (scope + ".json")).read_bytes() == (BASE / (scope + ".json")).read_bytes()
    for field in ("historical_census_complete", "historical_preregistration_proven",
            "collection_registration_created", "training_admissible", "formal_g2_pass"):
        assert report[field] is False


@pytest.mark.parametrize("damage", ["status_sha", "code_sha", "seed", "seed_bool", "duplicate",
    "missing_host", "missing_result", "wrong_host", "code_identity", "truncated", "winner", "score",
    "root_noise", "manifest_runner", "unfinished"])
def test_damaged_sampling_evidence_is_rejected(tmp_path, damage):
    for path in [SAMPLING / "run_host.py", *SAMPLING.glob("manifest-*.json")]:
        (tmp_path / path.name).write_bytes(path.read_bytes())
    records = deepcopy(json.loads((SAMPLING / "status-latest.json").read_bytes()))
    row = records[0]["rows"][0]
    if damage == "seed":
        row["seed"] += 1
    elif damage == "seed_bool":
        row["seed"] = True
    elif damage == "duplicate":
        records[0]["rows"].append(deepcopy(row))
    elif damage == "missing_host":
        records.pop()
    elif damage == "missing_result":
        records[0]["rows"].pop()
    elif damage == "wrong_host":
        records[0]["host"] = "35"
    elif damage == "code_identity":
        row["evaluation_identity"]["code_sha256"] = "f" * 64
    elif damage == "truncated":
        row["terminal"]["truncated"] = True
    elif damage == "winner":
        row["terminal"]["winner"] = False
    elif damage == "score":
        row["score"] = .25
    elif damage == "root_noise":
        row["root_noise"] = not row["root_noise"]
    elif damage == "manifest_runner":
        (tmp_path / "run_host.py").write_bytes(b"changed runner")
    elif damage == "unfinished":
        records[0]["status"]["stage"] = "evaluating"
    path = tmp_path / "status-latest.json"
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError):
        review_sampling(tmp_path, CODE, status_sha="0" * 64 if damage == "status_sha" else digest(path),
            code_sha="0" * 64 if damage == "code_sha" else digest(CODE))


def test_changed_base_audit_is_rejected_before_output(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        extend_inventory(BASE, EVIDENCE, output, base_audit_sha="0" * 64, sampling_directory=SAMPLING,
            code_path=CODE, status_sha=digest(SAMPLING / "status-latest.json"), code_sha=digest(CODE))
    assert not output.exists()
