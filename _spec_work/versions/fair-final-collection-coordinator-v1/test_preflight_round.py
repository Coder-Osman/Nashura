from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-integration-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

import preflight_round as coordinator
from seed_registration import make_registration, validate_registration
from test_seed_registration import registration_case
from native_runner.training.fair_final.storage import json_bytes
from native_runner.training.fair_search.contracts import semantic_digest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))
    return coordinator.file_identity(path)["sha256"]


@pytest.fixture
def round_case(registration_case, tmp_path):
    case = registration_case
    source = tmp_path / "source.pt"
    selected = tmp_path / "selected.pt"
    source.write_bytes(b"fixture source checkpoint")
    selected.write_bytes(b"fixture selected checkpoint")
    source_sha = coordinator.file_identity(source)["sha256"]
    selected_sha = coordinator.file_identity(selected)["sha256"]
    plan = deepcopy(case["plan"])
    old_source = plan["rounds"][0]["manifest"]["identity"]["source_a_sha"]
    for entry in plan["rounds"]:
        entry["manifest"]["identity"]["source_a_sha"] = source_sha
        for task in entry["manifest"]["matches"].values():
            task["behavior_checkpoint_shas"] = [source_sha if digest == old_source else digest
                for digest in task["behavior_checkpoint_shas"]]
    for task in plan["rounds"][-1]["manifest"]["matches"].values():
        task["behavior_checkpoint_shas"] = [source_sha if digest == source_sha else selected_sha
            for digest in task["behavior_checkpoint_shas"]]
    plan_path = tmp_path / "plan.json"
    write(plan_path, plan)
    registration = make_registration(plan, expected_plan_sha=semantic_digest(plan),
        inventory_paths=case["paths"], source_map_path=case["source_map"], source_paths=case["sources"])
    registration_path = tmp_path / "registration.json"
    write(registration_path, registration)
    matches = list(plan["rounds"][-1]["manifest"]["matches"])
    evidence = validate_registration(registration, expected_sha=semantic_digest(registration),
        plan=plan, expected_plan_sha=semantic_digest(plan), inventory_paths=case["paths"],
        source_map_path=case["source_map"], source_paths=case["sources"], match_id=matches[0])
    deployment = tmp_path / "deployment"
    integration = deployment / "_spec_work/versions/fair-final-migration-integration-v1"
    integration.mkdir(parents=True)
    collector = integration / "collect_selected.py"
    reports = {match: dict(schema="FairFinalSelectedCollectionPreflight/v1",
        status="passed_preflight_only", match_id=match, selected_checkpoint_sha=selected_sha,
        selected_network_sha="a" * 64, plan_sha=semantic_digest(plan),
        training_admissible=False, formal_g2_pass=False, **evidence) for match in matches}
    collector.write_text("import json,sys,time\nfrom pathlib import Path\n"
        "assert '--preflight-only' in sys.argv\n"
        "def value(flag):return sys.argv[sys.argv.index(flag)+1]\n"
        "report=" + repr(reports) + "[value('--match-id')]\n"
        "print(json.dumps(report),flush=True)\n")
    for name in ("selected_collection_worker.py", "seed_registration.py", "seed_sources.py"):
        (integration / name).write_text("raise RuntimeError('fixture must not launch worker')\n")
    (deployment / "phase_snapshot_launcher.py").write_text("raise RuntimeError('fixture must not start engine')\n")
    service = deployment / "project/research/standalone-resident-p18-stepatomic/service.py"
    service.parent.mkdir(parents=True)
    service.write_text("fixture engine service")
    probe = tmp_path / "probe.so"
    probe.write_bytes(b"fixture probe")
    identity = {path.relative_to(deployment).as_posix(): coordinator.file_identity(path)["sha256"]
        for path in deployment.rglob("*") if path.is_file()}
    deployment_sha = write(deployment / "deployment.json", identity)
    weights, config = tmp_path / "weights.json", tmp_path / "config.json"
    write(weights, {})
    write(config, {})
    parents = [tmp_path / ("parent-" + str(index)) for index in range(2)]
    for parent in parents:
        write(parent / "fixture.json", {"fixture": True})
    requests, roots = [], []
    for index, match in enumerate(matches):
        root = tmp_path / ("collection-" + str(index))
        roots.append(root)
        arguments = ["--engineering-acceptance", "--port", "29900", "--output", str(root / "data"),
            "--private-plan", str(plan_path), "--plan-sha", semantic_digest(plan),
            "--seed-registration", str(registration_path), "--seed-registration-sha", semantic_digest(registration),
            "--excluded-seed-inventories", *map(str, case["paths"]),
            "--seed-source-map", str(case["source_map"]), "--seed-source-artifacts", *map(str, case["sources"]),
            "--parent-data-roots", *map(str, parents), "--source", str(source), "--selected", str(selected),
            "--selected-sha", selected_sha, "--loss-weights", str(weights), "--selection-config", str(config),
            "--engine-sha", coordinator.file_identity(probe)["sha256"], "--match-id", match, "--device", "cpu"]
        request = dict(schema="FairFinalSelectedCollectionRun/v1", collector_args=arguments,
            engine=dict(project=str(deployment / "project"), probe=str(probe),
                launcher=str(deployment / "phase_snapshot_launcher.py"),
                launcher_sha256=identity["phase_snapshot_launcher.py"],
                service_sha256=coordinator.file_identity(service)["sha256"]))
        path = tmp_path / ("request-" + str(index) + ".json")
        write(path, request)
        requests.append(path)
    return dict(request_paths=requests, request_shas=[coordinator.file_identity(path)["sha256"] for path in requests],
        deployment_root=deployment, deployment_sha=deployment_sha, output=tmp_path / "prepared",
        collector=collector, roots=roots, parents=parents, source=source, selected=selected)


def execute(case, **options):
    return coordinator.prepare(**{key: case[key] for key in
        ("request_paths", "request_shas", "deployment_root", "deployment_sha", "output")} | options)


def change_request(case, index, edit):
    path = case["request_paths"][index]
    request = coordinator.read_json(path)
    edit(request["collector_args"])
    case["request_shas"][index] = write(path, request)


def change_collector(case, source):
    case["collector"].write_text(source)
    path = case["deployment_root"] / "deployment.json"
    identity = coordinator.read_json(path)
    identity[case["collector"].relative_to(case["deployment_root"]).as_posix()] = coordinator.file_identity(case["collector"])["sha256"]
    case["deployment_sha"] = write(path, identity)


def test_complete_round_checks_every_task_before_prepared_marker_without_engines(round_case):
    case = round_case
    inputs = {path: path.read_bytes() for path in case["request_paths"]}
    result = execute(case)
    assert result["status"] == "prepared_only"
    assert result["round"] == 2
    assert len(result["tasks"]) == 6
    assert all(not result[key] for key in ("native_collection_executed", "training_admissible", "formal_g2_pass", "gates_closed"))
    assert coordinator.read_json(case["output"] / "prepared.json") == result
    assert len(list(case["output"].glob("*-preflight.log"))) == 6
    assert not any(path.exists() for path in case["roots"])
    assert all(path.read_bytes() == data for path, data in inputs.items())
    for task in result["tasks"]:
        assert "selected_collection_worker.py" in task["worker_command"][3]
        assert task["request_sha256"] in task["worker_command"]
    with pytest.raises(FileExistsError):
        execute(case)


@pytest.mark.parametrize("damage", ["missing", "duplicate_task", "duplicate_request", "sha", "deployment",
    "source", "selected", "mixed_registration", "duplicate_option", "abbreviation", "attempt",
    "existing_root", "shared_root", "nested_root", "preflight_flag", "seed_source", "parent_link", "output_overlap"])
def test_static_failures_precede_subprocess_and_completion(round_case, damage, monkeypatch):
    case = round_case
    if damage == "missing":
        case["request_paths"].pop()
        case["request_shas"].pop()
    elif damage == "duplicate_task":
        first = coordinator.read_json(case["request_paths"][0])["collector_args"]
        change_request(case, 1, lambda args: args.__setitem__(args.index("--match-id") + 1, first[first.index("--match-id") + 1]))
    elif damage == "duplicate_request":
        case["request_paths"][1] = case["request_paths"][0]
        case["request_shas"][1] = case["request_shas"][0]
    elif damage == "sha":
        case["request_shas"][0] = "0" * 64
    elif damage == "deployment":
        case["deployment_sha"] = "0" * 64
    elif damage in ("source", "selected"):
        case[damage].write_bytes(b"changed")
    elif damage == "mixed_registration":
        change_request(case, 0, lambda args: args.__setitem__(args.index("--seed-registration-sha") + 1, "0" * 64))
    elif damage == "duplicate_option":
        change_request(case, 0, lambda args: args.extend(["--device", "cpu"]))
    elif damage == "abbreviation":
        change_request(case, 0, lambda args: args.__setitem__(args.index("--device"), "--dev"))
    elif damage == "attempt":
        change_request(case, 0, lambda args: args.extend(["--attempt-id", "2"]))
    elif damage == "existing_root":
        case["roots"][0].mkdir()
    elif damage in ("shared_root", "nested_root"):
        root = case["roots"][0]
        replacement = root / "data" if damage == "shared_root" else root / "nested/data"
        change_request(case, 1, lambda args: args.__setitem__(args.index("--output") + 1, str(replacement)))
    elif damage == "preflight_flag":
        change_request(case, 0, lambda args: args.append("--preflight-only"))
    elif damage == "seed_source":
        request = coordinator.read_json(case["request_paths"][0])
        args = request["collector_args"]
        Path(args[args.index("--seed-source-artifacts") + 1]).write_bytes(b"{}")
    elif damage == "parent_link":
        (case["parents"][0] / "link").symlink_to(case["source"])
    else:
        case["output"] = case["deployment_root"] / "coordinator"
    monkeypatch.setattr(coordinator.subprocess, "run", lambda *args, **kwargs: pytest.fail("static failure launched process"))
    with pytest.raises((ValueError, SystemExit)):
        execute(case)
    assert not case["output"].exists()


@pytest.mark.parametrize("damage", ["exit", "timeout", "malformed", "scope", "network", "change_input", "new_parent_file", "created_root"])
def test_dynamic_failures_keep_evidence_and_never_publish_prepared(round_case, damage):
    case = round_case
    original = case["collector"].read_text()
    if damage == "exit":
        source = "raise SystemExit(7)\n"
    elif damage == "timeout":
        source = "import time\ntime.sleep(3)\n"
    elif damage == "malformed":
        source = "print('not json')\n"
    elif damage == "scope":
        source = original.replace("print(json.dumps(report)", "report['training_admissible']=0\nprint(json.dumps(report)")
    elif damage == "network":
        source = original.replace("print(json.dumps(report)",
            "report['selected_network_sha']=report['match_id']\nprint(json.dumps(report)")
    elif damage == "change_input":
        source = original + "Path(value('--selected')).write_bytes(b'changed')\n"
    elif damage == "new_parent_file":
        source = original + "Path(" + repr(str(case["parents"][0] / "new.json")) + ").write_text('{}')\n"
    else:
        source = original + "Path(value('--output')).mkdir(parents=True)\n"
    change_collector(case, source)
    with pytest.raises((ValueError, subprocess.SubprocessError, TimeoutError)):
        execute(case, preflight_timeout=1 if damage == "timeout" else 10)
    assert (case["output"] / "failure.json").exists()
    assert not (case["output"] / "prepared.json").exists()
    assert not coordinator.read_json(case["output"] / "failure.json")["formal_g2_pass"]


@pytest.mark.parametrize("timeout", [0, -1, 14401, True, 1.5])
def test_phase_budget_is_bounded_and_typed(round_case, timeout):
    with pytest.raises(ValueError):
        execute(round_case, preflight_timeout=timeout)
    assert not round_case["output"].exists()
