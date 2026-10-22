from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest
import torch


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / name) for name in (
    "fair-final-migration-selection-v1", "fair-final-migration-series-v1",
    "fair-final-migration-integration-v1")]
sys.path.insert(0, str(ROOT.parents[2] / "firstlight_CR"))

from probe_native_resume import round_state
from round_selection import SelectionConfig, implementation_sha, resume_round
from test_round_selection import create
from test_series_training import WEIGHTS, series
from native_runner.training.fair_search.contracts import semantic_digest


spec = importlib.util.spec_from_file_location("active_content_verifier", ROOT / "verify_active.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
CONFIG = SelectionConfig(patience=2, min_delta=1e6)


@pytest.fixture(scope="module")
def comparison(series, tmp_path_factory):
    directory = tmp_path_factory.mktemp("active-content")
    run = create(series, config=CONFIG)
    identity = dict(dataset_identity=series["initial"].identity, implementation_sha=implementation_sha(),
        deployment_sha256="d" * 64, training_admissible=False, formal_g2_pass=False)
    reference = dict(**identity, mode="reference", initial=round_state(run), branches={}, updates=[])
    for stage, preparation in (("frozen", 1), ("trainable", 2)):
        for _ in range(preparation):
            reference["updates"].append(run.advance(series["initial"]))
        midpoint_sha = run.save(directory / (stage + "-midpoint.pt"))
        middle = round_state(run)
        metrics = [run.advance(series["initial"]) for _ in range(2)]
        expected = round_state(run)
        checkpoint_sha = run.save(directory / (stage + "-reference.pt"))
        reference["updates"].extend(metrics)
        reference["branches"][stage] = dict(midpoint_sha256=midpoint_sha, midpoint_state=middle,
            expected=expected, metrics=metrics, reference_checkpoint_sha256=checkpoint_sha)
        restored = resume_round(directory / (stage + "-midpoint.pt"), expected_sha=midpoint_sha,
            dataset=series["initial"], weights=WEIGHTS, config=CONFIG, bundle=series["bundle"])
        actual_metrics = [restored.advance(series["initial"]) for _ in range(2)]
        actual = round_state(restored)
        assert actual == expected and actual_metrics == metrics
        result = dict(**identity, mode=stage, status="passed_limited_scope", stage=stage, actual=actual,
            metrics=actual_metrics, exact_model_optimizer_rng_sampler_selection=True,
            resumed_checkpoint_sha256=restored.save(directory / (stage + "-resumed.pt")))
        (directory / (stage + ".json")).write_text(json.dumps(result))
    (directory / "reference.json").write_text(json.dumps(reference))
    return directory


def check(directory, series, **kwargs):
    reference = json.loads((directory / "reference.json").read_text())
    return verifier.verify_comparisons(directory,
        report_shas={name: verifier.sha(directory / (name + ".json"))
            for name in ("reference", "frozen", "trainable")},
        dataset=series["initial"], implementation_sha=implementation_sha(), deployment_sha="d" * 64,
        config=vars(CONFIG), weights=vars(WEIGHTS), teacher_sha=reference["initial"]["teacher"], **kwargs)


def test_two_stage_content_checks_actual_models_optimizer_rng_and_selection(comparison, series):
    report = check(comparison, series, native_schedule=False)
    assert report["branches"]["frozen"]["restored_updates"] == [1, 3]
    assert report["branches"]["trainable"]["final_steps"] == 7
    assert report["native_schedule_verified"] is False
    assert report["training_admissible"] is False and report["formal_g2_pass"] is False
    with pytest.raises(ValueError, match="real batch schedule"):
        check(comparison, series)


@pytest.mark.parametrize("change,error", (
    ("metrics", "update metrics"), ("stage", "scope or stage"),
    ("identity", "identity"), ("scope", "scope"), ("state", "checkpoint content"),
    ("schedule", "rows, stage or loss"), ("nan", "rows, stage or loss")))
def test_resealed_reports_rejected_against_checkpoint_content(comparison, series, tmp_path, change, error):
    directory = tmp_path / "copy"
    shutil.copytree(comparison, directory)
    name = "reference" if change in ("schedule", "nan") else "frozen"
    path = directory / (name + ".json")
    report = json.loads(path.read_text())
    if change == "metrics":
        report["metrics"][0]["loss"] += 1
    elif change == "stage":
        report["stage"] = "trainable"
    elif change == "identity":
        report["implementation_sha"] = "0" * 64
    elif change == "scope":
        report["formal_g2_pass"] = True
    elif change == "state":
        report["actual"]["optimizer"] = "0" * 64
    elif change == "schedule":
        report["updates"][0]["rows"] = 256
    else:
        report["updates"][0]["loss"] = float("nan")
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match=error):
        check(directory, series, native_schedule=False)


def test_checkpoint_bytes_rejected_before_trusting_report(comparison, series, tmp_path):
    directory = tmp_path / "copy"
    shutil.copytree(comparison, directory)
    path = directory / "trainable-resumed.pt"
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="file identity"):
        check(directory, series, native_schedule=False)


@pytest.mark.parametrize("change,error", (("cursor", "accounting"), ("order", "sampler RNG"),
    ("weights", "identity"), ("implementation", "identity")))
def test_resealed_checkpoint_cursor_and_configuration_rejected(comparison, series, tmp_path, change, error):
    path = tmp_path / "checkpoint.pt"
    envelope = deepcopy(torch.load(comparison / "frozen-midpoint.pt", weights_only=True))
    payload = envelope["payload"]
    trainer = payload["latest"]["payload"]
    if change == "cursor":
        trainer["cursor"]["consumed"] += 1
    elif change == "order":
        trainer["cursor"]["order"].reverse()
    elif change == "weights":
        trainer["weights"]["greedy_nll"] += 1
    else:
        payload["implementation_sha"] = "0" * 64
    payload["latest"]["sha256"] = semantic_digest(trainer)
    envelope["sha256"] = semantic_digest(payload)
    torch.save(envelope, path)
    with pytest.raises(ValueError, match=error):
        verifier.checked_checkpoint(path, verifier.sha(path), dataset=series["initial"],
            implementation_sha=implementation_sha(), config=vars(CONFIG), weights=vars(WEIGHTS),
            teacher_sha=trainer["teacher_network_sha"])


def test_pinned_report_digest_is_required(comparison, series):
    with pytest.raises(ValueError, match="pinned artifact"):
        verifier.verify_comparisons(comparison, report_shas={name: "0" * 64
            for name in ("reference", "frozen", "trainable")}, dataset=series["initial"],
            implementation_sha=implementation_sha(), deployment_sha="d" * 64, config=vars(CONFIG),
            weights=vars(WEIGHTS), teacher_sha="0" * 64, native_schedule=False)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    evidence = tmp_path / "native-evidence"
    evidence.mkdir()
    package = evidence / "payload.tar.gz"
    package.write_bytes(b"archive-fixture")
    phases = [dict(phase=name, exit_code=0, process=dict(pid=100 + index, start_ticks=str(index)))
        for index, name in enumerate(("benchmark", "reference", "frozen", "trainable"))]
    completion = dict(exit_code=0, training_admissible=False, formal_g2_pass=False)
    (evidence / "phase-results.json").write_text(json.dumps(phases))
    (evidence / "completion.json").write_text(json.dumps(completion))
    receipt = dict(files={})
    launch = dict(payload_sha256=verifier.sha(package))
    monkeypatch.setattr(verifier, "archived", lambda *args, **kwargs: (launch, evidence, receipt))
    return evidence, phases, completion, receipt


@pytest.mark.parametrize("change", ("exit", "duplicate_process", "missing_phase", "scope", "failure"))
def test_archive_rejects_failed_incomplete_or_same_process_execution(archive, change):
    evidence, phases, completion, receipt = archive
    if change == "exit":
        phases[-1]["exit_code"] = 1
    elif change == "duplicate_process":
        phases[-1]["process"] = phases[0]["process"]
    elif change == "missing_phase":
        phases.pop()
    elif change == "scope":
        completion["formal_g2_pass"] = True
    else:
        receipt["files"]["supervisor-failure.json"] = {}
    (evidence / "phase-results.json").write_text(json.dumps(phases))
    (evidence / "completion.json").write_text(json.dumps(completion))
    with pytest.raises(ValueError, match="quarantined|four successful"):
        verifier.verify_archive(evidence.parent, launch_sha="1" * 64, receipt_sha="2" * 64,
            data_roots=[evidence / "data"])


def test_archive_requires_receipt_coverage_before_reading_dataset(archive):
    evidence, _, _, _ = archive
    with pytest.raises(ValueError, match="omits comparison artifacts"):
        verifier.verify_archive(evidence.parent, launch_sha="1" * 64, receipt_sha="2" * 64,
            data_roots=[evidence / "data"])


def test_active_implementation_identity_binds_all_six_sources_and_pipeline(tmp_path):
    names = [verifier.SERIES + name for name in ("series_training.py", "verified_store.py",
        "deterministic_runtime.py", "migration_series.py")]
    names += ["firstlight_CR/native_runner/training/fair_migration_trainer.py",
        verifier.SELECTION + "round_selection.py", verifier.SELECTION + "train_round.py", *verifier.ACTIVE]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(name)
    original = verifier.implementation_identity(tmp_path)
    for name in names:
        path = tmp_path / name
        path.write_text(name + "changed")
        assert verifier.implementation_identity(tmp_path) != original
        path.write_text(name)
    assert verifier.implementation_identity(tmp_path) == original
