from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_remote_seed_history as audit_module
from audit_remote_seed_history import LOCAL_PREFIX, MANIFEST_SCHEMA, REMOTE_PREFIX, audit_remote_history
from native_runner.training.fair_final.storage import json_bytes


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json_bytes(document)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def fixture_sources(root, monkeypatch):
    sources = []
    for index, scope in enumerate(("training", "value_training", "evaluation")):
        relative = "local-" + scope + ".json"
        write_json(root / relative, dict(schema=MANIFEST_SCHEMA, jobs=[dict(match_seed=100 + index)]))
        sources.append((scope, relative, MANIFEST_SCHEMA, "match_seed"))
    monkeypatch.setattr(audit_module, "REVIEWED_SOURCES", tuple(sources))
    bundle, records = {}, []
    for number in (1, 2, 3):
        document = dict(schema=MANIFEST_SCHEMA, jobs=[dict(match_seed=100 + number), dict(match_seed=100 + number)])
        remote = REMOTE_PREFIX + f"round-{number:06d}/manifest.json"
        filename = f"round-{number:06d}-manifest.json"
        source = root / LOCAL_PREFIX / filename
        digest = write_json(source, document)
        bundle[remote] = document
        records.append(dict(remote=remote, local=filename, sha256=digest,
            bytes=source.stat().st_size, bundle_document_equal=True))
        if number == 1:
            write_json(root / "continuous-training-20261014/round-000001-manifest.json", document)
    bundle_path, receipt_path = root / "bundle.json", root / "receipt.json"
    bundle_sha = write_json(bundle_path, bundle)
    receipt = dict(schema="FairFinalReadOnlySeedSourceReceipt/v1", host="172.16.48.35",
        bundle_sha256=bundle_sha, files=records, historical_preregistration_proven=False,
        training_admissible=False, formal_g2_pass=False)
    receipt_sha = write_json(receipt_path, receipt)
    return dict(receipt_path=receipt_path, bundle_path=bundle_path,
        receipt_sha=receipt_sha, bundle_sha=bundle_sha)


def test_review_and_inventory_are_repeatable_and_preserve_scope(tmp_path, monkeypatch):
    inputs = fixture_sources(tmp_path, monkeypatch)
    output = tmp_path / "audit"
    report = audit_remote_history(tmp_path, output, **inputs)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert audit_remote_history(tmp_path, output, **inputs) == report
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
    assert report["unique_seeds_by_scope"] == dict(training=3, value_training=1, evaluation=1)
    assert report["unique_excluded_seeds"] == 4
    assert report["source_review"]["reviewed"][0]["existing_local_equivalent"] is True
    for field in ("historical_census_complete", "historical_preregistration_proven",
            "collection_registration_created", "training_admissible", "formal_g2_pass"):
        assert report[field] is False
    assert json.loads((output / "audit.json").read_bytes())["cross_scope_overlaps"]["evaluation:training"] == [102]


@pytest.mark.parametrize("damage", ["receipt_bytes", "bundle_bytes", "source_bytes", "file_size",
    "duplicate", "missing_round", "path", "bundle_document", "bundle_number_type", "round_one", "seed_bool", "scope"])
def test_review_rejects_changed_or_ambiguous_sources_before_output(tmp_path, monkeypatch, damage):
    inputs = fixture_sources(tmp_path, monkeypatch)
    receipt = json.loads(inputs["receipt_path"].read_bytes())
    bundle = json.loads(inputs["bundle_path"].read_bytes())
    source = tmp_path / LOCAL_PREFIX / "round-000002-manifest.json"
    if damage in ("receipt_bytes", "bundle_bytes", "source_bytes"):
        path = {"receipt_bytes": inputs["receipt_path"], "bundle_bytes": inputs["bundle_path"],
            "source_bytes": source}[damage]
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        if damage == "file_size":
            receipt["files"][1]["bytes"] += 1
        elif damage == "duplicate":
            receipt["files"][1] = deepcopy(receipt["files"][0])
        elif damage == "missing_round":
            receipt["files"].pop()
        elif damage == "path":
            receipt["files"][1]["local"] = "../round-000002-manifest.json"
        elif damage == "bundle_document":
            bundle[receipt["files"][1]["remote"]]["jobs"][0]["match_seed"] += 1
        elif damage == "bundle_number_type":
            job = bundle[receipt["files"][1]["remote"]]["jobs"][0]
            job["match_seed"] = float(job["match_seed"])
        elif damage == "round_one":
            write_json(tmp_path / "continuous-training-20261014/round-000001-manifest.json", {})
        elif damage == "seed_bool":
            document = json.loads(source.read_bytes())
            document["jobs"][0]["match_seed"] = True
            receipt["files"][1]["sha256"] = write_json(source, document)
            receipt["files"][1]["bytes"] = source.stat().st_size
            bundle[receipt["files"][1]["remote"]] = document
        elif damage == "scope":
            receipt["training_admissible"] = True
        inputs["bundle_sha"] = write_json(inputs["bundle_path"], bundle)
        receipt["bundle_sha256"] = inputs["bundle_sha"]
        inputs["receipt_sha"] = write_json(inputs["receipt_path"], receipt)
    output = tmp_path / "audit"
    with pytest.raises(ValueError):
        audit_remote_history(tmp_path, output, **inputs)
    assert not output.exists()


def test_changes_during_audit_never_publish_remote_completion(tmp_path, monkeypatch):
    inputs = fixture_sources(tmp_path, monkeypatch)
    original = audit_module.audit_history

    def change_receipt(*args, **kwargs):
        result = original(*args, **kwargs)
        path = inputs["receipt_path"]
        path.write_bytes(path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(audit_module, "audit_history", change_receipt)
    output = tmp_path / "audit"
    with pytest.raises(ValueError):
        audit_remote_history(tmp_path, output, **inputs)
    assert not (output / "remote-review-complete.json").exists()
