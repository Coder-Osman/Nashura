from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import statistics
import sys
import tarfile

import pytest
import torch

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"),
    str(ROOT.parent / "fair-final-migration-integration-v1"), str(ROOT.parents[2] / "firstlight_CR")]

import round_selection
import verify_pipeline as verifier
from probe_native_resume import reference_branch, resumed_branch
from test_round_selection import CONFIG, finish, initial_trainer
from test_series_training import series
from native_runner.training.fair_search.contracts import semantic_digest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def benchmark(identity, deployment_sha, rows):
    selected = [rows[0], rows[len(rows) // 2], rows[-1]]
    comparisons, timing = [], []
    for index in range(6):
        trial = index % 3
        metrics = dict(loss=2., conditional_policy_kl=1., greedy_nll=0., wdl_teacher_kl=1., rows=1.)
        comparisons.append(dict(row_id=selected[trial], frozen_encoder=index < 3,
            metrics={name: dict(metrics) for name in ("sequential", "batched")},
            maximum_gradient_delta=1e-7, legal_cells=[8]))
        names = ("sequential", "batched") if trial % 2 == 0 else ("batched", "sequential")
        for name in names:
            timing.append(dict(implementation=name, seconds=(index + 1) * (3. if name == "sequential" else 1.),
                row_id=selected[trial], trial=trial, frozen_encoder=index < 3))
    return dict(status="passed_limited_scope", training_admissible=False, formal_g2_pass=False,
        teacher_unchanged=True, global_rng_unchanged=True, dataset_identity=identity,
        deployment_sha256=deployment_sha, comparisons=comparisons, timing=timing,
        medians={name: statistics.median(item["seconds"] for item in timing if item["implementation"] == name)
            for name in ("sequential", "batched")}, paired_median_speedup=3.)


def report(run, dataset, checkpoint):
    return dict(schema="FairFinalMigrationSelectionReport/v1", checkpoint_sha=verifier.sha(checkpoint),
        implementation_sha=round_selection.implementation_sha(), config=vars(run.config),
        weights=vars(run.trainer.weights), dataset_identity=dataset.identity,
        series_sha=dataset.report["series_sha"], validation_sha=run.validation_sha,
        terminal=run.terminal, latest_optimizer_steps=run.trainer.optimizer_steps,
        selected=run.history[run.best_index], history=run.history,
        network_sha=semantic_digest(run.best["payload"]["network"]), training_admissible=False, formal_g2_pass=False)


def seal(attempt, launch):
    write(attempt / "launch.json", launch)
    evidence = attempt / "native-evidence"
    files = {str(path.relative_to(evidence)): dict(bytes=path.stat().st_size, sha256=verifier.sha(path))
        for path in evidence.rglob("*") if path.is_file()}
    write(attempt / "native-download-receipt.json", dict(timestamp=1, remote=launch["remote"],
        launch_sha256=verifier.sha(attempt / "launch.json"), files=files,
        training_admissible=False, formal_g2_pass=False))


@pytest.fixture(scope="module")
def pipeline(series, tmp_path_factory):
    attempt = tmp_path_factory.mktemp("pipeline-fixture")
    evidence = attempt / "native-evidence"
    paths = [verifier.SERIES + name for name in ("series_training.py", "verified_store.py",
        "deterministic_runtime.py", "migration_series.py")]
    paths += [verifier.SELECTION + name for name in ("round_selection.py", "train_round.py")]
    paths += ["firstlight_CR/native_runner/training/" + name for name in (
        "fair_migration_trainer.py", "fair_final/batched_migration.py", "fair_migration.py")]
    paths += [verifier.PROBE]
    for name in paths:
        path = evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source = ROOT.parents[2] / name
        if not source.exists():
            source = ROOT / Path(name).name if "batched_migration" in name else (
                ROOT.parent / "fair-final-migration-cache-v1" / Path(name).name)
        path.write_bytes(source.read_bytes())
    dataset = series["initial"]
    write(evidence / "private-plan.json", dataset.plan)
    write(evidence / "selection-config.json", vars(CONFIG))
    write(evidence / "loss-weights.json", vars(initial_trainer(series).weights))
    identity = {str(path.relative_to(evidence)): verifier.sha(path) for path in evidence.rglob("*") if path.is_file()}
    write(evidence / "deployment.json", identity)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(evidence.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(evidence)), recursive=False)
    (evidence / "payload.tar.gz").write_bytes(buffer.getvalue())
    implementation = verifier.implementation_identity(evidence)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(round_selection, "implementation_sha", lambda: implementation)
        run = round_selection.MigrationRound(initial_trainer(series), dataset, config=CONFIG)
        run.advance(dataset)
        first_sha = run.save(evidence / "round0.pt")
        write(evidence / "round0.report.json", report(run, dataset, evidence / "round0.pt"))
        comparison = evidence / "comparison"
        comparison.mkdir()
        reference = reference_branch(run, dataset, comparison)
        resumed_run = round_selection.resume_round(comparison / "midpoint.pt",
            expected_sha=reference["midpoint_sha256"], dataset=dataset, weights=run.trainer.weights,
            config=CONFIG, bundle=series["bundle"])
        resumed = resumed_branch(resumed_run, dataset, reference, comparison)
        for name, value, input_sha in (("reference", reference, first_sha),
                ("resumed", resumed, reference["midpoint_sha256"])):
            value.update(input_checkpoint_sha256=input_sha, dataset_identity=dataset.identity,
                probe_sha256=verifier.sha(evidence / verifier.PROBE))
            write(comparison / (name + ".json"), value)
        finish(run, dataset)
        run.save(evidence / "selected.pt")
        write(evidence / "selected.report.json", report(run, dataset, evidence / "selected.pt"))
    write(evidence / "benchmark/report.json", benchmark(dataset.identity,
        verifier.sha(evidence / "deployment.json"), dataset.splits["validation"]))
    write(evidence / "phase-results.json", [dict(phase=name, exit_code=0,
        process=dict(pid=index + 1, start_ticks=str(index + 10))) for index, name in enumerate(
            ("benchmark", "training", "reference", "resumed", "selection"))])
    write(evidence / "completion.json", dict(exit_code=0, training_admissible=False, formal_g2_pass=False))
    launch = dict(remote="/fixture/batch", source_sha256=identity,
        payload_sha256=verifier.sha(evidence / "payload.tar.gz"))
    seal(attempt, launch)
    return dict(attempt=attempt, data_roots=[store.root for store in dataset.stores])


def verify(paths):
    return verifier.verify_pipeline(**paths, launch_sha=verifier.sha(paths["attempt"] / "launch.json"),
        receipt_sha=verifier.sha(paths["attempt"] / "native-download-receipt.json"))


def test_complete_pipeline_fixture_rechecks_real_updates_and_terminal_selection(pipeline):
    result = verify(pipeline)
    assert result["content"]["steps"] == 3
    assert result["terminal"] == "EARLY_STOP"
    assert result["latest_optimizer_steps"] > 3
    assert result["benchmark"]["paired_median_speedup"] == 3.
    assert result["native_game_provenance_reverified"] is False
    assert result["training_admissible"] is False and result["gates_closed"] is False


@pytest.mark.parametrize("change", ["phase", "completion", "failure", "before", "selected_report",
    "timing", "pair_order", "metric", "benchmark_identity", "checkpoint_implementation", "checkpoint_split"])
def test_resealed_pipeline_corruption_rejected(pipeline, tmp_path, change):
    paths = dict(pipeline, attempt=tmp_path / "attempt")
    shutil.copytree(pipeline["attempt"], paths["attempt"])
    evidence = paths["attempt"] / "native-evidence"
    if change == "phase":
        path = evidence / "phase-results.json"
        value = json.loads(path.read_text())
        value[2]["process"] = value[0]["process"]
    elif change == "completion":
        path = evidence / "completion.json"
        value = dict(exit_code=124, training_admissible=False, formal_g2_pass=False)
    elif change == "failure":
        path, value = evidence / "supervisor-failure.json", dict(error="failed")
    elif change == "before":
        path = evidence / "comparison/reference.json"
        value = json.loads(path.read_text())
        value["before"]["optimizer"] = "0" * 64
    elif change == "selected_report":
        path = evidence / "selected.report.json"
        value = json.loads(path.read_text())
        value["selected"]["network_sha"] = "0" * 64
    elif change.startswith("checkpoint"):
        path = evidence / "selected.pt"
        envelope = torch.load(path, weights_only=True)
        value = envelope["payload"]
        if change == "checkpoint_implementation":
            value["implementation_sha"] = "0" * 64
        else:
            for key in ("latest", "best"):
                record = value[key]
                record["payload"]["splits"]["train"][0] = value["latest"]["payload"]["splits"]["test"][0]
                record["sha256"] = semantic_digest(record["payload"])
        envelope["sha256"] = semantic_digest(value)
        torch.save(envelope, path)
        report_path = evidence / "selected.report.json"
        value = json.loads(report_path.read_text())
        value["checkpoint_sha"] = verifier.sha(path)
        path = report_path
    else:
        path = evidence / "benchmark/report.json"
        value = json.loads(path.read_text())
        if change == "timing": value["paired_median_speedup"] = 100.
        elif change == "pair_order": value["timing"][0]["implementation"] = "batched"
        elif change == "metric": value["comparisons"][0]["metrics"]["batched"]["loss"] = 3.
        else: value["deployment_sha256"] = "0" * 64
    write(path, value)
    seal(paths["attempt"], json.loads((paths["attempt"] / "launch.json").read_text()))
    with pytest.raises(ValueError):
        verify(paths)
