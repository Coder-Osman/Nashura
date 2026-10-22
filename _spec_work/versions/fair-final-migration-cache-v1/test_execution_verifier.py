import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-integration-v1"),
    str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

from build_payload import build, SERIES, SELECTION
from test_cached_training import load_module
from test_round_selection import initial_trainer
from test_series_training import series
from probe_native_resume import reference_branch, resumed_branch
from verify_execution import verify_execution, sha, PROBE


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def seal(attempt, launch):
    write(attempt / "launch.json", launch)
    evidence = attempt / "native-evidence"
    files = {str(path.relative_to(evidence)): dict(bytes=path.stat().st_size, sha256=sha(path))
        for path in evidence.rglob("*") if path.is_file()}
    write(attempt / "native-download-receipt.json", dict(timestamp=1, remote=launch["remote"],
        launch_sha256=sha(attempt / "launch.json"), files=files, training_admissible=False, formal_g2_pass=False))


@pytest.fixture(scope="module")
def execution(series, tmp_path_factory):
    root = tmp_path_factory.mktemp("execution-evidence")
    parent, resumed_attempt = root / "parent", root / "resume"
    parent_evidence, evidence = parent / "native-evidence", resumed_attempt / "native-evidence"
    baseline = ROOT.parent / "fair-final-migration-integration-v1/native-selected-round0-1792629813912401000"
    package, _, _ = build(baseline)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    plan = series["initial"].plan
    files["private-plan.json"] = json.dumps(plan).encode()
    files["selection-config.json"] = json.dumps(dict(patience=2, min_delta=1e-6)).encode()
    files[PROBE] = (ROOT.parent / "fair-final-migration-integration-v1/probe_native_resume.py").read_bytes()
    identity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != "deployment.json"}
    files["deployment.json"] = json.dumps(identity).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            for destination in (parent_evidence, evidence):
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    package = stream.getvalue()
    (evidence / "payload.tar.gz").write_bytes(package)
    cached_series = load_module("evidence_cached_series", parent_evidence / SERIES / "series_training.py")
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "series_training", cached_series)
        selection = load_module("evidence_cached_selection", parent_evidence / SELECTION / "round_selection.py")
    dataset = cached_series.SeriesMigrationDataset(plan, [store.root for store in series["initial"].stores],
        teacher=series["teacher"])
    config = selection.SelectionConfig(patience=2, min_delta=1e-6)
    run = selection.MigrationRound(initial_trainer(series), dataset, config=config)
    run.advance(dataset)
    checkpoint_sha = run.save(parent_evidence / "round0.pt")
    report = dict(checkpoint_sha=checkpoint_sha, latest_optimizer_steps=1, terminal=None,
        dataset_identity=dataset.identity, series_sha=dataset.report["series_sha"])
    write(parent_evidence / "round0.report.json", report)
    write(parent_evidence / "completion.json", dict(exit_code=0, training_admissible=False))
    for name in ("round0.pt", "round0.report.json", "completion.json"):
        (evidence / ("parent-" + name)).write_bytes((parent_evidence / name).read_bytes())
    comparison = evidence / "comparison"
    comparison.mkdir()
    reference = reference_branch(run, dataset, comparison)
    restored = selection.resume_round(comparison / "midpoint.pt", expected_sha=reference["midpoint_sha256"],
        dataset=dataset, weights=run.trainer.weights, config=config, bundle=series["bundle"])
    resumed = resumed_branch(restored, dataset, reference, comparison)
    for name, value, input_sha in (("reference", reference, checkpoint_sha),
            ("resumed", resumed, reference["midpoint_sha256"])):
        value.update(input_checkpoint_sha256=input_sha, dataset_identity=dataset.identity,
            probe_sha256=sha(evidence / PROBE))
        write(comparison / (name + ".json"), value)
    parent_launch = dict(remote="/fixture/parent", process=dict(pid=1, start_ticks="10"),
        payload_sha256=hashlib.sha256(package).hexdigest(), source_sha256=identity,
        command=["python", "--source", "/fixture/source", "--data-roots", "/fixture/data"])
    seal(parent, parent_launch)
    settings = dict(parent=parent_launch["remote"], parent_process=parent_launch["process"],
        source="/fixture/source", data_root="/fixture/data")
    write(evidence / "resume-settings.json", settings)
    write(evidence / "parent-receipt.json", dict(checkpoint_sha256=checkpoint_sha,
        report_sha256=sha(parent_evidence / "round0.report.json"), completion_sha256=sha(parent_evidence / "completion.json"),
        parent=parent_launch["remote"], dataset_identity=dataset.identity))
    phases = [dict(phase=mode, exit_code=0, process=dict(pid=index + 2, start_ticks=str(index + 20)))
        for index, mode in enumerate(("reference", "resumed"))]
    write(evidence / "phase-results.json", phases)
    write(evidence / "completion.json", dict(status="passed_pending_independent_verification", phases=phases,
        training_admissible=False, formal_g2_pass=False))
    launch = dict(remote="/fixture/resume", parent_launch_sha256=sha(parent / "launch.json"), settings=settings,
        uploaded_sha256={"payload.tar.gz": hashlib.sha256(package).hexdigest(),
            "resume-settings.json": sha(evidence / "resume-settings.json")})
    seal(resumed_attempt, launch)
    return dict(parent=parent, attempt=resumed_attempt, data_roots=[store.root for store in series["initial"].stores])


def verify(paths, **options):
    return verify_execution(**paths, **options, parent_launch_sha=sha(paths["parent"] / "launch.json"),
        parent_receipt_sha=sha(paths["parent"] / "native-download-receipt.json"),
        resume_launch_sha=sha(paths["attempt"] / "launch.json"),
        resume_receipt_sha=sha(paths["attempt"] / "native-download-receipt.json"))


def test_complete_fixture_links_parent_deployment_data_and_checkpoint(execution):
    result = verify(execution)
    assert result["content"]["steps"] == 3
    assert result["frozen_deployment_verified"] is True
    assert result["native_game_provenance_reverified"] is False
    assert result["formal_g2_pass"] is False


def test_fixture_cannot_be_presented_as_native_game_provenance(execution):
    with pytest.raises(ValueError, match="native data provenance path"):
        verify(execution, native_data_attempt=execution["data_roots"][0])


@pytest.mark.parametrize("change", ["parent_before", "phase", "snapshot", "deployment", "data_split"])
def test_resealed_evidence_changes_are_rejected(execution, tmp_path, change):
    import shutil
    paths = dict(execution)
    for name in ("parent", "attempt"):
        paths[name] = tmp_path / name
        shutil.copytree(execution[name], paths[name])
    evidence = paths["attempt"] / "native-evidence"
    if change == "parent_before":
        path = evidence / "comparison/reference.json"
        value = json.loads(path.read_text())
        value["before"]["optimizer"] = "0" * 64
    elif change == "phase":
        path = evidence / "phase-results.json"
        value = json.loads(path.read_text())
        value[1]["process"] = value[0]["process"]
        completion = json.loads((evidence / "completion.json").read_text())
        completion["phases"] = value
        write(evidence / "completion.json", completion)
    elif change == "snapshot":
        path = evidence / "parent-completion.json"
        value = dict(exit_code=124, training_admissible=False)
    elif change == "deployment":
        path = evidence / "deployment.json"
        value = json.loads(path.read_text())
        value[PROBE] = "0" * 64
    else:
        paths["data_roots"] = [tmp_path / "data"]
        shutil.copytree(execution["data_roots"][0], paths["data_roots"][0])
        path = paths["data_roots"][0] / "dataset.json"
        value = json.loads(path.read_text())
        next(iter(value["matches"].values()))["split"] = "test"
    write(path, value)
    launch = json.loads((paths["attempt"] / "launch.json").read_text())
    seal(paths["attempt"], launch)
    with pytest.raises(ValueError):
        verify(paths)
