import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path, PurePosixPath
import sys


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-batch-v1")]

from collect_selected import argument_parser
from migration_series import ReadOnlyMigrationStore, validate_series
from seed_registration import validate_registration
from verify_pipeline import implementation_identity, verified_dataset, verify_selection
from verify_execution import pinned, read
from verify_native_resume import sha
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest
from tools.fair_migration_supplement import validate_supplement_round


INTEGRATION = "_spec_work/versions/fair-final-migration-integration-v1/"
RUNTIME = "_spec_work/versions/fair-final-migration-series-v1/deterministic_runtime.py"


def deployment_identity(root, expected_sha):
    root = Path(root)
    pinned(root / "deployment.json", expected_sha)
    identity = read(root / "deployment.json")
    required = {INTEGRATION + name for name in (
        "collect_selected.py", "selected_collection_worker.py", "seed_registration.py", "seed_sources.py")}
    if not required.union({"phase_snapshot_launcher.py"}).issubset(identity):
        raise ValueError("collection deployment dependencies absent")
    for name, digest in identity.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("noncanonical collection deployment path")
        if (root / name).is_symlink() or not (root / name).resolve().is_relative_to(root.resolve()):
            raise ValueError("collection deployment escapes archived root")
        pinned(root / name, digest)
    return identity


def process_key(value):
    if (set(value) != {"pid", "start_ticks"} or type(value["pid"]) is not int
            or value["pid"] <= 0 or not isinstance(value["start_ticks"], str)
            or not value["start_ticks"].isdigit()):
        raise ValueError("explicit Linux process identity required")
    return value["pid"], value["start_ticks"]


def verify_runtime(private, identity):
    runtime = private.get("deterministic_runtime", {})
    expected = dict(event="deterministic_runtime_configured", deterministic=True, warn_only=False,
        cudnn_deterministic=True, cudnn_benchmark=False, matmul_allow_tf32=False,
        cudnn_allow_tf32=False, cublas_workspace_config=":4096:8")
    if (RUNTIME not in identity or private.get("deterministic_runtime_sha") != identity[RUNTIME]
            or set(runtime) != {*expected, "torch", "cuda"}
            or any(semantic_digest(runtime[key]) != semantic_digest(value) for key, value in expected.items())
            or not isinstance(runtime["torch"], str) or not runtime["torch"]
            or (runtime["cuda"] is not None and (not isinstance(runtime["cuda"], str) or not runtime["cuda"]))):
        raise ValueError("archived deterministic runtime declaration differs")


def verify_lifecycle(root, request, options):
    identities = []
    for phase in ("preflight", "collection"):
        record = read(root / (phase + "-process.json"))
        if (type(record["exit_code"]) is not int or record["exit_code"] != 0
                or type(record["seconds"]) not in (int, float)
                or not math.isfinite(record["seconds"]) or record["seconds"] <= 0):
            raise ValueError("unsuccessful collection lifecycle phase")
        identities.append(process_key(record["process"]))
    launcher = read(root / "engine-process.json")
    identities.append(process_key(launcher))
    ready, cleanup = read(root / "engine-ready.json"), read(root / "engine-cleanup.json")
    stopped = cleanup["process"]
    engine = request["engine"]
    attestation = dict(path=str(PurePosixPath(engine["probe"])), sha256=options.engine_sha,
        launcher_sha256=engine["launcher_sha256"], service_sha256=engine["service_sha256"])
    for state in (ready, stopped):
        if ({key: state[key] for key in launcher} != launcher
                or state.get("phase_snapshot_engine") != attestation):
            raise ValueError("owned engine identity or attestation differs")
        process_key(dict(pid=state["child_pid"], start_ticks=state["child_start_ticks"]))
    if (ready.get("ready") is not True or stopped.get("phase") != "stopped"
            or cleanup.get("launcher") != launcher or cleanup.get("clean") is not True
            or cleanup.get("port_released") is not True
            or ready["child_pid"] != stopped["child_pid"]
            or ready["child_start_ticks"] != stopped["child_start_ticks"]):
        raise ValueError("engine readiness, owned child or cleanup differs")
    identities.append((ready["child_pid"], ready["child_start_ticks"]))
    if len(set(identities)) != len(identities):
        raise ValueError("collection lifecycle reused a process identity")


def verify_episode(root, *, plan, options, private_expected, network_sha, source_manifest, identity):
    manifest = plan["rounds"][-1]["manifest"]
    store = ReadOnlyMigrationStore(root / "data", manifest=manifest)
    episode, receipt = store.load(options.match_id)
    if receipt["attempt_id"] != options.attempt_id:
        raise ValueError("completed receipt belongs to another attempt")
    audit = store.root / "audit" / options.match_id / str(options.attempt_id)
    private = read(audit / "private-inputs.json")
    if any(key not in private or semantic_digest(private[key]) != semantic_digest(value)
            for key, value in private_expected.items()):
        raise ValueError("collection private lineage, seeds or implementation differs")
    verify_runtime(private, identity)
    registration_root = audit / "seed-registration"
    registration = read(registration_root / "registration.json")
    evidence = validate_registration(registration, expected_sha=options.seed_registration_sha,
        plan=plan, expected_plan_sha=options.plan_sha, match_id=options.match_id,
        inventory_paths=[registration_root / (entry["file_sha256"] + ".json")
            for entry in registration["inventory_identities"]],
        source_map_path=registration_root / "source-map.json",
        source_paths=list((registration_root / "sources").glob("*.json")))
    if any(private.get(key) != value for key, value in evidence.items()):
        raise ValueError("private seed provenance differs from archived source artifacts")
    raw = read(audit / "native-terminal.json")
    result = episode["result"]
    if (type(raw.get("tick")) is not int or raw["tick"] != result["terminal_tick"]
            or any(key not in raw or semantic_digest(raw[key]) != semantic_digest(value)
                for key, value in result["native_evidence"].items())):
        raise ValueError("raw native terminal differs from published episode")
    report = read(store.root / "attempts" / options.match_id / str(options.attempt_id) / "report.json")
    expected = dict(schema="fair-final-supplementary-migration-episode.v1", status="passed",
        match_id=options.match_id, attempt_id=options.attempt_id, round=manifest["round"], receipt=receipt,
        terminal=result, rows=len(episode["rows"]),
        rows_by_owner={str(owner): sum(row["owner"] == owner for row in episode["rows"]) for owner in (0, 1)},
        lifecycle=dict(Counter(row["outcome"]["reason"] for row in episode["rows"])),
        student_owner=manifest["matches"][options.match_id]["behavior_checkpoint_shas"].index(options.selected_sha),
        student_checkpoint_sha=options.selected_sha, student_network_sha=network_sha,
        source_network_sha=manifest["identity"]["source_network_sha"],
        decoding="student-greedy-versus-A-adapter-greedy", training_admissible=False,
        source_sha256=source_manifest, engine_sha256=options.engine_sha,
        integration_entrypoint_sha=private_expected["integration_entrypoint_sha"],
        preregistered_plan_sha=options.plan_sha, parent_series_sha=private_expected["parent_series_sha"])
    if (any(key not in report or semantic_digest(report[key]) != semantic_digest(value)
            for key, value in expected.items())
            or report.get("training_admissible") is not False
            or report.get("publication") != dict(status="published", semantic_sha=receipt["semantic_sha"])):
        raise ValueError("collector report differs from complete episode content")
    return episode, receipt


def verify_collection(root, *, deployment_root, request_sha, deployment_sha, completion_sha,
        plan_path, plan_sha, selected_path, selected_sha, selected_report_path,
        weights_path, config_path, parent_data_roots):
    root, deployment_root = Path(root), Path(deployment_root)
    for name in ("failure.json", "cleanup-failure.json"):
        if (root / name).exists():
            raise ValueError("failed collection is quarantined")
    pinned(root / "request.json", request_sha)
    pinned(root / "completion.json", completion_sha)
    identity = deployment_identity(deployment_root, deployment_sha)
    request = read(root / "request.json")
    if (set(request) != {"schema", "collector_args", "engine"}
            or request["schema"] != "FairFinalSelectedCollectionRun/v1"
            or not isinstance(request["collector_args"], list)
            or any(not isinstance(item, str) for item in request["collector_args"])):
        raise ValueError("collection request schema differs")
    options = argument_parser().parse_args(request["collector_args"])
    if (options.preflight_only or options.plan_sha != plan_sha or options.selected_sha != selected_sha
            or type(options.attempt_id) is not int or not 1 <= options.attempt_id <= 3):
        raise ValueError("collection request inputs differ")
    engine = request["engine"]
    if (set(engine) != {"project", "probe", "launcher", "launcher_sha256", "service_sha256"}
            or engine["launcher_sha256"] != identity["phase_snapshot_launcher.py"]):
        raise ValueError("collection launcher identity differs")
    completion = read(root / "completion.json")
    report_path = root / "data/attempts" / options.match_id / str(options.attempt_id) / "report.json"
    expected = dict(schema="FairFinalSelectedCollectionRunResult/v1",
        status="collected_requires_independent_verification", request_sha256=request_sha,
        deployment_sha256=deployment_sha, report_sha256=sha(report_path), match_id=options.match_id,
        training_admissible=False, formal_g2_pass=False)
    if (any(completion.get(key) != value for key, value in expected.items())
            or completion.get("training_admissible") is not False or completion.get("formal_g2_pass") is not False
            or type(completion.get("seconds")) not in (int, float)
            or not math.isfinite(completion["seconds"]) or completion["seconds"] <= 0):
        raise ValueError("collection completion identity or scope differs")
    plan = read(plan_path)
    if semantic_digest(plan) != plan_sha:
        raise ValueError("collection registered plan differs")
    validate_series(plan)
    if len(plan["rounds"]) not in (2, 3):
        raise ValueError("supplementary registered round required")
    validate_supplement_round(plan["rounds"][-1]["manifest"], plan["rounds"][-2]["manifest"], selected_sha)
    parent_plan = deepcopy(plan)
    parent_plan["rounds"].pop()
    dataset, parent_audit, source = verified_dataset(parent_plan, parent_data_roots)
    implementation = implementation_identity(deployment_root)
    selection_report = read(selected_report_path)
    if selection_report["checkpoint_sha"] != selected_sha:
        raise ValueError("selected parent checksum differs from request")
    _, payload = verify_selection(selected_path, selection_report, dataset=dataset,
        implementation_sha=implementation, config=read(config_path), weights=read(weights_path),
        series_sha=parent_audit["series_sha"])
    if payload["terminal"] is None:
        raise ValueError("collection requires completed parent selection")
    source_manifest = {name.removeprefix("firstlight_CR/"): digest for name, digest in identity.items()
        if name.endswith(".py") and "tests" not in Path(name).parts
        and (name.startswith("firstlight_CR/native_runner/") or name == "firstlight_CR/tools/fair_migration_supplement.py")}
    private = dict(plan["rounds"][-1]["private_tasks"][options.match_id],
        purpose="selected-supplementary-migration-engineering-acceptance", source_sha256=source["source_a_sha"],
        student_checkpoint_sha256=selected_sha, student_network_sha=selection_report["network_sha"],
        parent_dataset_sha=dataset.identity["dataset_sha"], parent_series_sha=parent_audit["series_sha"],
        preregistered_plan_sha=plan_sha, selection_implementation_sha=implementation,
        source_sha256_manifest=source_manifest, integration_entrypoint_sha=identity[INTEGRATION + "collect_selected.py"],
        seed_registration_implementation_sha=identity[INTEGRATION + "seed_registration.py"],
        seed_sources_implementation_sha=identity[INTEGRATION + "seed_sources.py"],
        training_admissible=False, formal_g2_pass=False)
    verify_lifecycle(root, request, options)
    episode, receipt = verify_episode(root, plan=plan, options=options, private_expected=private,
        network_sha=selection_report["network_sha"], source_manifest=source_manifest, identity=identity)
    return dict(schema="FairFinalSelectedCollectionVerification/v1",
        status="passed_archived_collection_content_scope", match_id=options.match_id,
        round=plan["rounds"][-1]["manifest"]["round"], rows=len(episode["rows"]),
        receipt_sha=semantic_digest(receipt), request_sha256=request_sha, deployment_sha256=deployment_sha,
        completion_sha256=completion_sha, selected_checkpoint_sha=selected_sha,
        terminal_tick=episode["result"]["terminal_tick"], teacher_targets_recomputed=False,
        scope="archived_source_process_terminal_episode_seed_and_selected_parent_consistency",
        training_admissible=False, formal_g2_pass=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    for name in ("deployment-root", "plan-path", "selected-path", "selected-report-path", "weights-path", "config-path"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("request-sha", "deployment-sha", "completion-sha", "plan-sha", "selected-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--parent-data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = vars(parser.parse_args())
    output = args.pop("output")
    report = verify_collection(**args)
    report["verifier_sha256"] = sha(__file__)
    durable_write(output, json_bytes(report))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
