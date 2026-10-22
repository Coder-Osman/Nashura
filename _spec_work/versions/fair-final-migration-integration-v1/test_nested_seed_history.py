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

from audit_nested_seed_history import extend_inventory, review_readback, review_stage
from collect_nested_seed_history import SLOTS


EVIDENCE = WORKSPACE / "_spec_work/evidence"
FINAL = EVIDENCE / "fair-final-v1-20261019"
READBACK = FINAL / "historical-seed-nested-readback-20261022"
BASE = FINAL / "historical-seed-inventory-policy-screen-20261022"
RECEIPT_SHA = "991b735e51174390df5f77804862d750ae8a36d07a2c69970873d2ca1f785737"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arguments():
    previous = json.loads((FINAL / "historical-seed-inventory-remote-final-20261022/audit.json").read_bytes())
    return dict(base_audit_sha=digest(BASE / "audit.json"),
        source_paths=[EVIDENCE / row["path"] for row in previous["reviewed_sources"]] +
            [EVIDENCE / "teacher-sampling-eval-20261013/status-latest.json",
             EVIDENCE / "new-value-policy-screen-20261013/status-latest.json"],
        readback=READBACK, receipt_sha=RECEIPT_SHA)


def stage_documents(slot="clean-screen"):
    documents = [json.loads((READBACK / (slot + "-" + suffix + ".json")).read_bytes())
        for suffix in ("manifest", "results", "done")]
    stage, pairs = next((stage, pairs) for name, _, stage, pairs in SLOTS if name == slot)
    return documents, dict(stage=stage, pairs=pairs, code_sha=digest(READBACK / (slot + "-evaluate_policy.py")))


def test_real_readback_projects_explicit_seeds_without_wdl_admission():
    review, seeds = review_readback(READBACK, receipt_sha=RECEIPT_SHA)
    assert len(seeds) == 1600
    assert sum(stage["result_rows"] for stage in review["stages"]) == 3200
    assert sum(len(stage["ambiguous_outcome_jobs"]) for stage in review["stages"]) == 5
    assert all(stage["wdl_training_admissible"] is False for stage in review["stages"])
    assert all(stage["native_terminal_reverified"] is False for stage in review["stages"])
    assert all(stage["execution_code_record_matches"] is True for stage in review["stages"])
    assert sum(len(stage["source_projection"]["engine_seed_pointers"])
        for stage in review["stages"]) == 3200


def test_extension_is_immutable_preserves_scopes_and_source_evidence(tmp_path):
    report = extend_inventory(BASE, tmp_path, **arguments())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert extend_inventory(BASE, tmp_path, **arguments()) == report
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert report["unique_excluded_seeds"] == 7160
    assert report["added_evaluation_seeds"] == 1600
    assert report["unique_seeds_by_scope"] == dict(training=1136, value_training=4096, evaluation=1928)
    assert not any(report["cross_scope_overlaps"].values())
    for scope in ("training", "value_training"):
        assert (tmp_path / (scope + ".json")).read_bytes() == (BASE / (scope + ".json")).read_bytes()
    for field in ("historical_census_complete", "historical_preregistration_proven",
            "collection_registration_created", "training_admissible", "formal_g2_pass"):
        assert report[field] is False


@pytest.mark.parametrize("damage", ["pairs_bool", "seed_bool", "seed_overflow", "missing_job",
    "duplicate_job", "job_pair_bool", "host", "job_id", "missing_result", "duplicate_result",
    "result_pair_bool", "owner_bool", "attempt_bool", "attempt_exhausted", "seed", "model",
    "execution_code", "decoder", "identity_pair_bool", "identity_owner_bool", "terminal_winner_bool",
    "winner", "tick_bool", "tick_mismatch", "rejected_bool", "unconfirmed", "score_bool",
    "score", "duration_nan", "ended", "truncated", "reason", "missing_done", "promoted",
    "report_count", "report_count_bool", "report_score", "report_interval"])
def test_semantically_damaged_stage_is_rejected(damage):
    documents, options = stage_documents()
    manifest, results, done = documents
    job, row = manifest["jobs"][0], results["matches"][0]
    if damage == "pairs_bool":
        manifest["pairs"] = True
    elif damage == "seed_bool":
        manifest["seed_base"] = True
    elif damage == "seed_overflow":
        manifest["seed_base"] = (1 << 32) - 1
    elif damage == "missing_job":
        manifest["jobs"].pop()
    elif damage == "duplicate_job":
        manifest["jobs"].append(deepcopy(job))
    elif damage == "job_pair_bool":
        job["pair"] = False
    elif damage == "host":
        job["host"] = "99"
    elif damage == "job_id":
        job["id"] = "other"
    elif damage == "missing_result":
        results["matches"].pop()
    elif damage == "duplicate_result":
        results["matches"].append(deepcopy(row))
    elif damage == "result_pair_bool":
        row["pair"] = False
    elif damage == "owner_bool":
        row["candidate_owner"] = True
    elif damage == "attempt_bool":
        row["attempt"] = False
    elif damage == "attempt_exhausted":
        row["attempt"] = 3
    elif damage == "seed":
        row["seed"] += 1
    elif damage == "model":
        row["candidate_sha256"] = "f" * 64
    elif damage == "execution_code":
        row["evaluation_identity"]["code_sha256"] = "f" * 64
    elif damage == "decoder":
        row["evaluation_identity"]["mode"] = "search"
    elif damage == "identity_pair_bool":
        row["evaluation_identity"]["pairs"] = True
    elif damage == "identity_owner_bool":
        row["evaluation_identity"]["owners"] = [True]
    elif damage == "terminal_winner_bool":
        row["terminal"]["winner"] = True
    elif damage == "winner":
        row["winner"] = 1 - row["winner"]
    elif damage == "tick_bool":
        row["terminal"]["terminal_tick"] = True
    elif damage == "tick_mismatch":
        row["terminal_tick"] += 1
    elif damage == "rejected_bool":
        row["terminal"]["rejected"] = False
    elif damage == "unconfirmed":
        row["terminal"]["unconfirmed"] = 1
    elif damage == "score_bool":
        row["score"] = True
    elif damage == "score":
        row["score"] = .25
    elif damage == "duration_nan":
        row["wall_seconds"] = float("nan")
    elif damage == "ended":
        row["terminal"]["ended"] = 1
    elif damage == "truncated":
        row["terminal"]["truncated"] = True
    elif damage == "reason":
        row["terminal"]["reason"] = "timeout"
    elif damage == "missing_done":
        done.clear()
    elif damage == "promoted":
        results["report"]["promoted"] = done["promoted"] = True
    elif damage == "report_count":
        results["report"]["wins"] = done["wins"] = 0
    elif damage == "report_count_bool":
        results["report"]["draws"] = done["draws"] = False
    elif damage == "report_score":
        results["report"]["paired"]["score"] = done["paired"]["score"] = .5
    else:
        results["report"]["paired"]["lower95"] = done["paired"]["lower95"] = float("nan")
    with pytest.raises(ValueError):
        review_stage(*documents, **options)


def test_legacy_ambiguous_outcomes_are_never_upgraded_to_true_draws():
    documents, options = stage_documents("clean-confirmation")
    ambiguous = next(row for row in documents[1]["matches"] if row["winner"] is None)
    ambiguous["terminal"]["reason"] = "native_final_result"
    with pytest.raises(ValueError, match="terminal"):
        review_stage(*documents, **options)


@pytest.mark.parametrize("damage", ["receipt_sha", "source_bytes", "source_sha", "missing_file",
    "duplicate_file", "remote_path", "scope", "code_bytes"])
def test_readback_pins_files_even_after_receipt_rehash(tmp_path, damage):
    for path in READBACK.iterdir():
        (tmp_path / path.name).write_bytes(path.read_bytes())
    receipt = json.loads((tmp_path / "receipt.json").read_bytes())
    record = receipt["files"][0]
    if damage == "source_bytes":
        record["bytes"] += 1
    elif damage == "source_sha":
        record["sha256"] = "f" * 64
    elif damage == "missing_file":
        receipt["files"].pop()
    elif damage == "duplicate_file":
        receipt["files"].append(deepcopy(record))
    elif damage == "remote_path":
        record["remote"] += ".other"
    elif damage == "scope":
        receipt["training_admissible"] = True
    elif damage == "code_bytes":
        record = next(row for row in receipt["files"] if row["local"].endswith(".py"))
        path = tmp_path / record["local"]
        path.write_bytes(path.read_bytes() + b"\n")
        record.update(sha256=digest(path), bytes=path.stat().st_size)
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        review_readback(tmp_path, receipt_sha="0" * 64 if damage == "receipt_sha"
            else digest(tmp_path / "receipt.json"))


@pytest.mark.parametrize("damage", ["audit_sha", "missing_source", "wrong_map", "identity", "counts"])
def test_base_identity_is_verified_before_creating_output(tmp_path, damage):
    base = tmp_path / "base"
    base.mkdir()
    for name in ("audit", "source-map", "training", "value_training", "evaluation"):
        (base / (name + ".json")).write_bytes((BASE / (name + ".json")).read_bytes())
    options = arguments()
    if damage == "audit_sha":
        options["base_audit_sha"] = "0" * 64
    elif damage == "missing_source":
        options["source_paths"].pop()
    elif damage == "wrong_map":
        mapping = json.loads((base / "source-map.json").read_bytes())
        mapping["sources"][0]["engine_seed_pointers"][0] = "/jobs/0/search_seed"
        (base / "source-map.json").write_text(json.dumps(mapping))
    else:
        audit = json.loads((base / "audit.json").read_bytes())
        if damage == "identity":
            audit["inventory_identities"] = []
        else:
            audit["unique_excluded_seeds"] += 1
        (base / "audit.json").write_text(json.dumps(audit))
        options["base_audit_sha"] = digest(base / "audit.json")
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        extend_inventory(base, output, **options)
    assert not output.exists()
