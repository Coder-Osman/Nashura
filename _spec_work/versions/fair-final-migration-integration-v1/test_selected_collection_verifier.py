from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-batch-v1")]

import verify_selected_collection as verifier
from seed_registration import SCOPES, archive_registration, make_registration, validate_registration
from test_pipeline_verifier import pipeline, report, write
from test_series_training import new_trainer, series
from test_round_selection import CONFIG, finish
from round_selection import MigrationRound
from native_runner.tests.test_fair_migration_data import collect
from native_runner.training.fair_migration_data import MigrationStore
from native_runner.training.fair_search.contracts import semantic_digest


@pytest.fixture(scope="module")
def archived_collection(pipeline, series, tmp_path_factory):
    root = tmp_path_factory.mktemp("collection-verifier")
    deployment = root / "deployment"
    original = pipeline["attempt"] / "native-evidence"
    shutil.copytree(original, deployment)
    identity = verifier.read(deployment / "deployment.json")
    dependencies = [verifier.INTEGRATION + name for name in (
        "collect_selected.py", "selected_collection_worker.py", "seed_registration.py", "seed_sources.py")]
    dependencies.append("firstlight_CR/tools/fair_migration_supplement.py")
    for name in dependencies:
        target = deployment / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT.parents[2] / name, target)
        identity[name] = verifier.sha(target)
    launcher = deployment / "phase_snapshot_launcher.py"
    launcher.write_text("fixture_launcher_identity = True\n")
    identity["phase_snapshot_launcher.py"] = verifier.sha(launcher)
    write(deployment / "deployment.json", identity)
    plan = deepcopy(series["plan"])
    selected_sha = verifier.sha(deployment / "selected.pt")
    for task in plan["rounds"][-1]["manifest"]["matches"].values():
        task["behavior_checkpoint_shas"] = [selected_sha if model == series["digest"] else model
            for model in task["behavior_checkpoint_shas"]]
    plan_sha = semantic_digest(plan)
    write(root / "plan.json", plan)
    manifest = plan["rounds"][-1]["manifest"]
    match = next(iter(manifest["matches"]))
    source = manifest["identity"]
    staging_manifest = deepcopy(manifest)
    staging_manifest.update(round=0, matches={match: deepcopy(manifest["matches"][match])})
    staging_manifest["matches"][match]["behavior_checkpoint_shas"] = [source["source_a_sha"]] * 2
    staging = MigrationStore(root / "staging", manifest=staging_manifest)
    collect(staging, series["teacher"], "train", preempt=True)
    episode, _ = staging.load(match)
    run = root / "run"
    store = MigrationStore(run / "data", manifest=manifest)
    episode["dataset_sha"] = store.dataset_sha
    publication = store.publish(episode)
    episode, receipt = store.load(match)
    audit = store.root / "audit" / match / "1"
    audit.mkdir(parents=True)
    inventories, sources, mapping = [], [], []
    for scope in sorted(SCOPES):
        artifact = root / (scope + "-source.json")
        write(artifact, dict(scope=scope, engine_seeds=[]))
        digest = verifier.sha(artifact)
        sources.append(artifact)
        mapping.append(dict(file_sha256=digest, engine_seed_pointers=["/engine_seeds"]))
        inventory = root / (scope + "-inventory.json")
        write(inventory, dict(schema="FairFinalSeedExclusion/v1", scope=scope,
            engine_seeds=[], source_shas=[digest]))
        inventories.append(inventory)
    source_map = root / "source-map.json"
    write(source_map, dict(schema="FairFinalSeedSources/v1", sources=mapping))
    registration = make_registration(plan, expected_plan_sha=plan_sha, inventory_paths=inventories,
        source_map_path=source_map, source_paths=sources)
    registration_sha = semantic_digest(registration)
    archive_registration(registration, expected_sha=registration_sha, inventory_paths=inventories,
        source_map_path=source_map, source_paths=sources, output=audit / "seed-registration")
    provenance = validate_registration(registration, expected_sha=registration_sha, plan=plan,
        expected_plan_sha=plan_sha, inventory_paths=inventories, source_map_path=source_map,
        source_paths=sources, match_id=match)
    parent_dataset, parent_audit, _ = verifier.verified_dataset(series["initial"].plan, pipeline["data_roots"])
    selected = verifier.read(deployment / "selected.report.json")
    source_manifest = {name.removeprefix("firstlight_CR/"): digest for name, digest in identity.items()
        if name.startswith("firstlight_CR/") and name.endswith(".py")}
    private = dict(plan["rounds"][-1]["private_tasks"][match],
        purpose="selected-supplementary-migration-engineering-acceptance", source_sha256=source["source_a_sha"],
        student_checkpoint_sha256=selected_sha, student_network_sha=selected["network_sha"],
        parent_dataset_sha=parent_dataset.identity["dataset_sha"], parent_series_sha=parent_audit["series_sha"],
        preregistered_plan_sha=plan_sha, selection_implementation_sha=verifier.implementation_identity(deployment),
        source_sha256_manifest=source_manifest, integration_entrypoint_sha=identity[verifier.INTEGRATION + "collect_selected.py"],
        seed_registration_implementation_sha=identity[verifier.INTEGRATION + "seed_registration.py"],
        seed_sources_implementation_sha=identity[verifier.INTEGRATION + "seed_sources.py"],
        training_admissible=False, formal_g2_pass=False, **provenance)
    private.update(deterministic_runtime=dict(event="deterministic_runtime_configured", torch="fixture",
        cuda=None, deterministic=True, warn_only=False, cudnn_deterministic=True, cudnn_benchmark=False,
        matmul_allow_tf32=False, cudnn_allow_tf32=False, cublas_workspace_config=":4096:8"),
        deterministic_runtime_sha=identity[verifier.RUNTIME])
    write(audit / "private-inputs.json", private)
    write(audit / "native-terminal.json", dict(episode["result"]["native_evidence"], tick=102))
    engine_sha = "a" * 64
    collection_report = dict(schema="fair-final-supplementary-migration-episode.v1", status="passed",
        match_id=match, attempt_id=1, round=1, receipt=receipt, terminal=episode["result"], rows=4,
        rows_by_owner={"0": 2, "1": 2}, lifecycle=dict(Counter(row["outcome"]["reason"] for row in episode["rows"])),
        student_owner=0, student_checkpoint_sha=selected_sha, student_network_sha=selected["network_sha"],
        source_network_sha=source["source_network_sha"], decoding="student-greedy-versus-A-adapter-greedy",
        training_admissible=False, source_sha256=source_manifest, engine_sha256=engine_sha,
        integration_entrypoint_sha=private["integration_entrypoint_sha"], preregistered_plan_sha=plan_sha,
        parent_series_sha=parent_audit["series_sha"], publication=publication)
    report_path = store.root / "attempts" / match / "1/report.json"
    write(report_path, collection_report)
    arguments = ["--engineering-acceptance"]
    for name, value in dict(port=29771, output="/remote/run/data", private_plan="/remote/plan.json",
            plan_sha=plan_sha, seed_registration="/remote/registration.json", seed_registration_sha=registration_sha,
            excluded_seed_inventories="/remote/inventory.json", seed_source_map="/remote/source-map.json",
            seed_source_artifacts="/remote/source.json", parent_data_roots="/remote/round0",
            source="/remote/source.pt", selected="/remote/selected.pt", selected_sha=selected_sha,
            loss_weights="/remote/weights.json", selection_config="/remote/config.json", engine_sha=engine_sha,
            match_id=match, attempt_id=1).items():
        arguments.extend(["--" + name.replace("_", "-"), str(value)])
    request = dict(schema="FairFinalSelectedCollectionRun/v1", collector_args=arguments,
        engine=dict(project="/remote/project", probe="/remote/probe.so", launcher="/remote/phase_snapshot_launcher.py",
            launcher_sha256=identity["phase_snapshot_launcher.py"], service_sha256="b" * 64))
    write(run / "request.json", request)
    for ordinal, phase in enumerate(("preflight", "collection")):
        write(run / (phase + "-process.json"), dict(process=dict(pid=ordinal + 1, start_ticks=str(ordinal + 1)),
            exit_code=0, seconds=1.))
    launcher_process = dict(pid=3, start_ticks="3")
    state = dict(launcher_process, child_pid=4, child_start_ticks="4", ready=True, phase="ready",
        phase_snapshot_engine=dict(path="/remote/probe.so", sha256=engine_sha,
            launcher_sha256=identity["phase_snapshot_launcher.py"], service_sha256="b" * 64))
    write(run / "engine-process.json", launcher_process)
    write(run / "engine-ready.json", state)
    write(run / "engine-cleanup.json", dict(launcher=launcher_process, exit_code=0,
        process=dict(state, phase="stopped"), port_released=True, clean=True))
    completion = dict(schema="FairFinalSelectedCollectionRunResult/v1", status="collected_requires_independent_verification",
        request_sha256=verifier.sha(run / "request.json"), deployment_sha256=verifier.sha(deployment / "deployment.json"),
        report_sha256=verifier.sha(report_path), match_id=match, seconds=3., training_admissible=False, formal_g2_pass=False)
    write(run / "completion.json", completion)
    return dict(root=run, deployment_root=deployment, request_sha=completion["request_sha256"],
        deployment_sha=completion["deployment_sha256"], completion_sha=verifier.sha(run / "completion.json"),
        plan_path=root / "plan.json", plan_sha=plan_sha, selected_path=deployment / "selected.pt",
        selected_sha=selected_sha, selected_report_path=deployment / "selected.report.json",
        weights_path=deployment / "loss-weights.json", config_path=deployment / "selection-config.json",
        parent_data_roots=pipeline["data_roots"])


def test_archived_selected_collection_rechecks_content_with_no_remote_reads(archived_collection):
    result = verifier.verify_collection(**archived_collection)
    assert result["status"] == "passed_archived_collection_content_scope"
    assert result["rows"] == 4 and result["terminal_tick"] == 102
    assert not any(result[name] for name in ("teacher_targets_recomputed", "training_admissible", "formal_g2_pass", "gates_closed"))


@pytest.mark.parametrize("damage", ["rows", "owner", "lifecycle", "network", "report_terminal", "private_seed",
    "private_model", "private_implementation", "private_typed_false", "private_runtime", "private_runtime_sha",
    "private_runtime_type", "raw_terminal", "source_bytes", "completion_duration", "completion_duration_type",
    "failed_phase", "phase_typed_zero", "unclean", "reused_process", "child_changed", "attestation",
    "failure", "cleanup_failure", "request_pin", "deployment_pin", "completion_pin", "selected_pin", "missing_parent"])
def test_resealed_collection_tampering_rejected(archived_collection, tmp_path, damage):
    options = dict(archived_collection, root=tmp_path / "run")
    shutil.copytree(archived_collection["root"], options["root"])
    root = options["root"]
    report_path = next((root / "data/attempts").glob("*/*/report.json"))
    audit = next((root / "data/audit").glob("*/*"))
    if damage in ("rows", "owner", "lifecycle", "network", "report_terminal"):
        value = verifier.read(report_path)
        if damage == "rows": value["rows"] += 1
        elif damage == "owner": value["student_owner"] = 1
        elif damage == "lifecycle": value["lifecycle"] = {}
        elif damage == "network": value["student_network_sha"] = "0" * 64
        else: value["terminal"]["terminal_tick"] += 1
        write(report_path, value)
        completion = verifier.read(root / "completion.json")
        completion["report_sha256"] = verifier.sha(report_path)
        write(root / "completion.json", completion)
        options["completion_sha"] = verifier.sha(root / "completion.json")
    elif damage.startswith("private_"):
        value = verifier.read(audit / "private-inputs.json")
        if damage == "private_seed": value["engine_seed"] += 1
        elif damage == "private_model": value["student_checkpoint_sha256"] = "0" * 64
        elif damage == "private_implementation": value["selection_implementation_sha"] = "0" * 64
        elif damage == "private_runtime": value["deterministic_runtime"]["deterministic"] = False
        elif damage == "private_runtime_sha": value["deterministic_runtime_sha"] = "0" * 64
        elif damage == "private_runtime_type": value["deterministic_runtime"]["deterministic"] = 1
        else: value["training_admissible"] = 0
        write(audit / "private-inputs.json", value)
    elif damage in ("completion_duration", "completion_duration_type"):
        value = verifier.read(root / "completion.json")
        value["seconds"] = -1 if damage == "completion_duration" else True
        write(root / "completion.json", value)
        options["completion_sha"] = verifier.sha(root / "completion.json")
    elif damage == "raw_terminal":
        value = verifier.read(audit / "native-terminal.json")
        value["tick"] += 1
        write(audit / "native-terminal.json", value)
    elif damage == "source_bytes":
        path = next((audit / "seed-registration/sources").glob("*.json"))
        path.write_bytes(path.read_bytes() + b"\n")
    elif damage in ("failed_phase", "phase_typed_zero"):
        path = root / "collection-process.json"
        value = verifier.read(path)
        value["exit_code"] = 1 if damage == "failed_phase" else False
        write(path, value)
    elif damage in ("unclean", "child_changed", "attestation"):
        path = root / "engine-cleanup.json"
        value = verifier.read(path)
        if damage == "unclean": value["clean"] = False
        elif damage == "child_changed": value["process"]["child_pid"] = 7
        else: value["process"]["phase_snapshot_engine"]["sha256"] = "0" * 64
        write(path, value)
    elif damage == "reused_process":
        path = root / "collection-process.json"
        value = verifier.read(path)
        value["process"] = verifier.read(root / "preflight-process.json")["process"]
        write(path, value)
    elif damage in ("failure", "cleanup_failure"):
        write(root / (damage.replace("_", "-") + ".json"), dict(error="fixture failure"))
    elif damage.endswith("_pin"):
        options[damage.removesuffix("_pin") + "_sha"] = "0" * 64
    else:
        options["parent_data_roots"] = []
    with pytest.raises(ValueError):
        verifier.verify_collection(**options)


@pytest.mark.parametrize("damage", ["traversal", "symlink", "bytes"])
def test_deployment_content_rejected_with_resealed_manifest(archived_collection, tmp_path, damage):
    deployment = tmp_path / "deployment"
    shutil.copytree(archived_collection["deployment_root"], deployment)
    identity = verifier.read(deployment / "deployment.json")
    name = verifier.INTEGRATION + "collect_selected.py"
    if damage == "traversal":
        identity["../outside.py"] = "0" * 64
    elif damage == "symlink":
        target = tmp_path / "outside.py"
        (deployment / name).rename(target)
        (deployment / name).symlink_to(target)
    else:
        (deployment / name).write_bytes((deployment / name).read_bytes() + b"\n")
    write(deployment / "deployment.json", identity)
    with pytest.raises(ValueError):
        verifier.deployment_identity(deployment, verifier.sha(deployment / "deployment.json"))


def test_readonly_multi_round_dataset_matches_training_identity(series):
    dataset, audit, _ = verifier.verified_dataset(series["plan"], series["roots"])
    assert dataset.identity == series["combined"].identity
    assert dataset.splits == series["combined"].splits
    assert audit["series_sha"] == series["combined"].report["series_sha"]


def test_completed_supplement_selection_preserves_parent_step_offset(series, tmp_path):
    run = MigrationRound(new_trainer(series), series["combined"], config=CONFIG)
    finish(run, series["combined"])
    path = tmp_path / "supplement-selected.pt"
    run.save(path)
    expected = report(run, series["combined"], path)
    dataset, audit, _ = verifier.verified_dataset(series["plan"], series["roots"])
    state, payload = verifier.verify_selection(path, expected, dataset=dataset,
        implementation_sha=expected["implementation_sha"], config=vars(CONFIG), weights=vars(run.trainer.weights),
        series_sha=audit["series_sha"])
    assert state["steps"] > payload["latest"]["payload"]["lineage"]["parent_optimizer_steps"] > 0
