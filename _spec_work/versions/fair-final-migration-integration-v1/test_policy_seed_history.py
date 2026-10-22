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

from audit_policy_seed_history import extend_inventory, review_policy_screen


EVIDENCE = WORKSPACE / "_spec_work/evidence"
SCREEN = EVIDENCE / "new-value-policy-screen-20261013"
BASE = EVIDENCE / "fair-final-v1-20261019/historical-seed-inventory-sampling-20261022"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs():
    previous = json.loads((EVIDENCE /
        "fair-final-v1-20261019/historical-seed-inventory-remote-final-20261022/audit.json").read_bytes())
    return dict(base_audit_sha=digest(BASE / "audit.json"),
        source_paths=[EVIDENCE / row["path"] for row in previous["reviewed_sources"]] +
            [EVIDENCE / "teacher-sampling-eval-20261013/status-latest.json"],
        screen_directory=SCREEN, status_sha=digest(SCREEN / "status-latest.json"),
        export_sha=digest(SCREEN / "export.json"))


def test_real_policy_screen_is_seed_exclusion_only():
    report, seeds = review_policy_screen(SCREEN,
        status_sha=digest(SCREEN / "status-latest.json"), export_sha=digest(SCREEN / "export.json"))
    assert seeds == set(range(17613000, 17613200))
    assert report["result_rows"] == 400
    assert len(report["source_projection"]["engine_seed_pointers"]) == 400
    assert report["execution_code_identity_proven"] is False
    assert report["native_terminal_reverified"] is False


def test_extension_preserves_original_scopes_and_reentry(tmp_path):
    report = extend_inventory(BASE, tmp_path, **inputs())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert extend_inventory(BASE, tmp_path, **inputs()) == report
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert report["unique_excluded_seeds"] == 5560
    assert report["unique_seeds_by_scope"] == dict(training=1136, value_training=4096, evaluation=328)
    assert report["added_evaluation_seeds"] == 200
    assert not any(report["cross_scope_overlaps"].values())
    for scope in ("training", "value_training"):
        assert (tmp_path / (scope + ".json")).read_bytes() == (BASE / (scope + ".json")).read_bytes()
    for field in ("historical_census_complete", "historical_preregistration_proven",
            "collection_registration_created", "training_admissible", "formal_g2_pass"):
        assert report[field] is False


@pytest.mark.parametrize("damage", ["seed", "seed_bool", "duplicate", "missing_result",
    "missing_host", "wrong_host", "missing_shard", "assignment", "candidate", "winner_bool",
    "score", "tick", "unfinished", "failed_exit", "missing_exit", "duplicate_exit",
    "engine_live", "status_sha", "export_sha"])
def test_damaged_screen_is_rejected(tmp_path, damage):
    records = deepcopy(json.loads((SCREEN / "status-latest.json").read_bytes()))
    (tmp_path / "export.json").write_bytes((SCREEN / "export.json").read_bytes())
    host = records[0]
    shard = host["shards"][0]
    row = shard["matches"][0]
    if damage == "seed":
        row["seed"] += 1
    elif damage == "seed_bool":
        row["seed"] = True
    elif damage == "duplicate":
        shard["matches"].append(deepcopy(row))
    elif damage == "missing_result":
        shard["matches"].pop()
    elif damage == "missing_host":
        records.pop()
    elif damage == "wrong_host":
        host["host"] = "35"
    elif damage == "missing_shard":
        host["shards"].pop()
    elif damage == "assignment":
        shard["start_pair"] += 1
    elif damage == "candidate":
        shard["identity.json"]["candidate_sha256"] = "f" * 64
    elif damage == "winner_bool":
        row["winner"] = False
    elif damage == "score":
        row["score"] = .25
    elif damage == "tick":
        row["terminal_tick"] = False
    elif damage == "unfinished":
        host["status.json"]["stage"] = "running"
    elif damage == "failed_exit":
        host["status.json"]["exits"][0]["exit_code"] = 1
        host["done.json"]["exits"][0]["exit_code"] = 1
    elif damage == "missing_exit":
        host["status.json"]["exits"].pop()
        host["done.json"]["exits"].pop()
    elif damage == "duplicate_exit":
        for key in ("status.json", "done.json"):
            host[key]["exits"].append(deepcopy(host[key]["exits"][0]))
    elif damage == "engine_live":
        shard["engine_ready"] = True
    status = tmp_path / "status-latest.json"
    status.write_text(json.dumps(records))
    with pytest.raises(ValueError):
        review_policy_screen(tmp_path, status_sha="0" * 64 if damage == "status_sha" else digest(status),
            export_sha="0" * 64 if damage == "export_sha" else digest(tmp_path / "export.json"))


@pytest.mark.parametrize("damage", ["base_audit", "missing_source", "wrong_source", "wrong_map"])
def test_base_source_identity_is_checked_before_publication(tmp_path, damage):
    base = tmp_path / "base"
    base.mkdir()
    for name in ("audit", "source-map", "training", "value_training", "evaluation"):
        (base / (name + ".json")).write_bytes((BASE / (name + ".json")).read_bytes())
    arguments = inputs()
    if damage == "base_audit":
        arguments["base_audit_sha"] = "0" * 64
    elif damage == "missing_source":
        arguments["source_paths"].pop()
    elif damage == "wrong_source":
        arguments["source_paths"][-1] = SCREEN / "status-latest.json"
    else:
        mapping = json.loads((base / "source-map.json").read_bytes())
        mapping["sources"][0]["engine_seed_pointers"][0] = "/jobs/0/search_seed"
        (base / "source-map.json").write_text(json.dumps(mapping))
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        extend_inventory(base, output, **arguments)
    assert not output.exists()
