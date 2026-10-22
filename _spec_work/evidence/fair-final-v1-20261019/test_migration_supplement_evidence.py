import json
from pathlib import Path
import shutil

import pytest

from verify_migration_supplement import ROOT, validate_supplement_evidence


ATTEMPT = ROOT / "native-migration-supplement-1792556420626023000"


@pytest.fixture
def evidence(tmp_path):
    destination = tmp_path / "evidence"
    shutil.copytree(ATTEMPT, destination)
    return destination


def change(path, update):
    value = json.loads(path.read_text())
    update(value)
    path.write_text(json.dumps(value))


def first_episode_report(evidence):
    return next((evidence / "data/attempts").glob("*/1/report.json"))


def test_native_supplement_verifies_both_seats_without_formal_gate():
    report = validate_supplement_evidence(ATTEMPT)
    assert report["status"] == "engineering_verified"
    assert {case["student_owner"] for case in report["cases"]} == {0, 1}
    assert report["training_admissible"] is False and report["formal_g2_pass"] is False
    assert report["rows"] == sum(case["rows"] for case in report["cases"])


@pytest.mark.parametrize("bad", ["completion", "training", "formal", "worker", "source", "checkpoint",
    "missing_seat", "cross_split", "private_seed", "parent_dataset", "terminal", "student_owner",
    "network", "rows", "lifecycle", "decoding", "quarantine", "cleanup", "summary"])
def test_native_supplement_rejects_missing_or_inconsistent_evidence(evidence, bad):
    report_path = first_episode_report(evidence)
    match_id = report_path.parent.parent.name
    audit = evidence / "data/audit" / match_id / "1"
    if bad == "completion":
        change(evidence / "completion.json", lambda value: value.update(exit_code=1))
    elif bad == "training":
        change(evidence / "launch.json", lambda value: value.update(training_admissible=True))
    elif bad == "formal":
        change(evidence / "report.json", lambda value: value.update(formal_g2_pass=True))
    elif bad == "worker":
        with (evidence / "worker.py").open("a") as handle:
            handle.write("\nchanged=True\n")
    elif bad == "source":
        change(evidence / "deployed-source.json", lambda value: value.pop(next(iter(value))))
    elif bad == "checkpoint":
        change(evidence / "launch.json", lambda value: value.update(checkpoint_sha256="d" * 64))
    elif bad == "missing_seat":
        change(evidence / "manifest.json", lambda value: value["matches"].pop(next(iter(value["matches"]))))
    elif bad == "cross_split":
        change(evidence / "manifest.json", lambda value: next(iter(value["matches"].values())).update(split="validation"))
    elif bad == "private_seed":
        change(audit / "private-inputs.json", lambda value: value.update(engine_seed=7))
    elif bad == "parent_dataset":
        change(audit / "private-inputs.json", lambda value: value.update(parent_dataset_sha="d" * 64))
    elif bad == "terminal":
        change(audit / "native-terminal.json", lambda value: value.update(tick=value["tick"] + 1))
    elif bad == "student_owner":
        change(report_path, lambda value: value.update(student_owner=1 - value["student_owner"]))
    elif bad == "network":
        change(report_path, lambda value: value.update(student_network_sha="d" * 64))
    elif bad == "rows":
        change(report_path, lambda value: value.update(rows=value["rows"] + 1))
    elif bad == "lifecycle":
        change(report_path, lambda value: value.update(lifecycle={}))
    elif bad == "decoding":
        change(report_path, lambda value: value.update(decoding="sampled"))
    elif bad == "quarantine":
        (report_path.parent / "quarantine.json").write_text("{}")
    elif bad == "cleanup":
        change(evidence / "engine-cleanup.json", lambda value: value[0].update(port_released=False))
    elif bad == "summary":
        change(evidence / "report.json", lambda value: value.update(reports=[]))
    with pytest.raises(ValueError):
        validate_supplement_evidence(evidence)
