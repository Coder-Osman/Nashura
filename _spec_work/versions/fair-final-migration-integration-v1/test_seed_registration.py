from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-series-v1")]

from seed_registration import SCOPES, archive_registration, main, make_registration, validate_registration
from test_migration_series import fixture_plan
from native_runner.training.fair_final.storage import json_bytes
from native_runner.training.fair_search.contracts import semantic_digest


@pytest.fixture
def registration_case(tmp_path):
    plan = fixture_plan()
    paths = []
    sources, entries = [], []
    for index, scope in enumerate(sorted(SCOPES)):
        source = tmp_path / (scope + "-source.json")
        source.write_bytes(json_bytes(dict(engine_seeds=[7000 + index])))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        sources.append(source)
        entries.append(dict(file_sha256=digest, engine_seed_pointers=["/engine_seeds"]))
        path = tmp_path / (scope + ".json")
        path.write_bytes(json_bytes(dict(schema="FairFinalSeedExclusion/v1", scope=scope,
            engine_seeds=[7000 + index], source_shas=[digest])))
        paths.append(path)
    source_map = tmp_path / "source-map.json"
    source_map.write_bytes(json_bytes(dict(schema="FairFinalSeedSources/v1", sources=entries)))
    plan["excluded_engine_seeds"] = [7000, 7001, 7002]
    registration = make_registration(plan, expected_plan_sha=semantic_digest(plan), inventory_paths=paths,
        source_map_path=source_map, source_paths=sources)
    return dict(plan=plan, paths=paths, registration=registration, source_map=source_map, sources=sources)


def validate(case, *, registration=None, expected_sha=None, plan=None, paths=None, match_id=None):
    current = case["registration"] if registration is None else registration
    current_plan = case["plan"] if plan is None else plan
    return validate_registration(current,
        expected_sha=semantic_digest(current) if expected_sha is None else expected_sha,
        plan=current_plan, expected_plan_sha=semantic_digest(current_plan),
        inventory_paths=case["paths"] if paths is None else paths,
        source_map_path=case["source_map"], source_paths=case["sources"],
        match_id=next(iter(current_plan["rounds"][-1]["manifest"]["matches"])) if match_id is None else match_id)


def test_round_two_registration_binds_all_inputs_without_mutating_them(registration_case):
    case = registration_case
    before = deepcopy(case["plan"])
    data = {path: path.read_bytes() for path in case["paths"]}
    result = validate(case, paths=list(reversed(case["paths"])))
    assert result["seed_registration_sha"] == semantic_digest(case["registration"])
    assert result["seed_overlap_scope"] == "provided_source_artifacts_and_explicit_json_pointers_only"
    assert case["registration"]["registered_round"] == 2
    assert case["registration"]["training_admissible"] is False
    assert before == case["plan"]
    assert data == {path: path.read_bytes() for path in case["paths"]}


@pytest.mark.parametrize("damage", ["sha", "round", "plan", "private_seed", "match", "extra",
    "training_admissible", "typed_false", "scope", "file_bytes", "source_sha", "seed",
    "missing_scope", "duplicate_inventory"])
def test_registration_rejects_identity_and_external_inventory_changes(registration_case, damage):
    case = registration_case
    registration = deepcopy(case["registration"])
    options = dict(registration=registration)
    if damage == "sha":
        options["expected_sha"] = "0" * 64
    elif damage == "round":
        registration["registered_round"] = 1
    elif damage == "plan":
        registration["plan_sha"] = "0" * 64
    elif damage == "private_seed":
        plan = deepcopy(case["plan"])
        for task in plan["rounds"][-1]["private_tasks"].values():
            task["execution_seed"] += 1
        options["plan"] = plan
    elif damage == "match":
        options["match_id"] = "0" * 64
    elif damage == "extra":
        registration["hidden"] = []
    elif damage in ("training_admissible", "typed_false"):
        registration["training_admissible"] = True if damage == "training_admissible" else 0
    elif damage == "scope":
        registration["external_overlap_scope"] = "all_history"
    elif damage == "missing_scope":
        options["paths"] = case["paths"][:-1]
    elif damage == "duplicate_inventory":
        options["paths"] = case["paths"] + case["paths"][:1]
    elif damage == "file_bytes":
        path = case["paths"][0]
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path = case["paths"][0]
        inventory = json.loads(path.read_bytes())
        inventory["source_shas" if damage == "source_sha" else "engine_seeds"] = ["f" * 64] if damage == "source_sha" else [9000]
        path.write_bytes(json_bytes(inventory))
    with pytest.raises(ValueError):
        validate(case, **options)


@pytest.mark.parametrize("damage", ["overlap", "omitted_exclusion", "boolean", "overflow",
    "duplicate_seed", "missing_source", "bad_source", "extra_field", "bad_schema"])
def test_inventory_must_be_well_formed_and_excluded_by_the_series(registration_case, damage):
    case = registration_case
    path = case["paths"][0]
    inventory = json.loads(path.read_bytes())
    if damage == "overlap":
        inventory["engine_seeds"] = [next(iter(case["plan"]["rounds"][-1]["private_tasks"].values()))["engine_seed"]]
        case["plan"]["excluded_engine_seeds"] += inventory["engine_seeds"]
    elif damage == "omitted_exclusion":
        case["plan"]["excluded_engine_seeds"] = []
    elif damage in ("boolean", "overflow", "duplicate_seed"):
        inventory["engine_seeds"] = {"boolean": [True], "overflow": [1 << 32], "duplicate_seed": [7000, 7000]}[damage]
    elif damage == "missing_source":
        inventory["source_shas"] = []
    elif damage == "bad_source":
        inventory["source_shas"] = ["bad"]
    elif damage == "extra_field":
        inventory["opponent_hand"] = []
    else:
        inventory["schema"] = "other"
    path.write_bytes(json_bytes(inventory))
    with pytest.raises(ValueError):
        make_registration(case["plan"], expected_plan_sha=semantic_digest(case["plan"]), inventory_paths=case["paths"],
            source_map_path=case["source_map"], source_paths=case["sources"])


def test_registration_cli_is_immutable_and_idempotent(registration_case, tmp_path, monkeypatch):
    case = registration_case
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(json_bytes(case["plan"]))
    output = tmp_path / "registration.json"
    argv = ["seed_registration.py", "--engineering-acceptance", "--private-plan", str(plan_path),
        "--plan-sha", semantic_digest(case["plan"]), "--excluded-seed-inventories",
        *map(str, case["paths"]), "--seed-source-map", str(case["source_map"]),
        "--seed-source-artifacts", *map(str, case["sources"]), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", argv)
    main()
    original = output.read_bytes()
    main()
    assert output.read_bytes() == original
    inventory = json.loads(case["paths"][0].read_bytes())
    inventory["source_shas"] = ["f" * 64]
    case["paths"][0].write_bytes(json_bytes(inventory))
    with pytest.raises(ValueError, match="no verified projection"):
        main()
    assert output.read_bytes() == original


@pytest.mark.parametrize("damage", ["registration", "source", "mapping"])
def test_collection_checks_registration_before_models_or_native(registration_case, tmp_path, monkeypatch, damage):
    import collect_selected
    case = registration_case
    plan_path, registration_path = tmp_path / "plan.json", tmp_path / "registration.json"
    plan_path.write_bytes(json_bytes(case["plan"]))
    registration_path.write_bytes(json_bytes(case["registration"]))
    def forbidden(*args, **kwargs):
        pytest.fail("model or native engine reached before registration check")
    monkeypatch.setattr(collect_selected, "load_checkpoint", forbidden)
    monkeypatch.setattr(collect_selected, "create_search_native", forbidden)
    digest = "0" * 64 if damage == "registration" else semantic_digest(case["registration"])
    if damage in ("source", "mapping"):
        path = case["sources"][0] if damage == "source" else case["source_map"]
        path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setattr(sys, "argv", ["collect_selected.py", "--engineering-acceptance", "--port", "1",
        "--output", str(tmp_path / "output"), "--private-plan", str(plan_path),
        "--plan-sha", semantic_digest(case["plan"]), "--seed-registration", str(registration_path),
        "--seed-registration-sha", digest, "--excluded-seed-inventories", *map(str, case["paths"]),
        "--seed-source-map", str(case["source_map"]), "--seed-source-artifacts", *map(str, case["sources"]),
        "--parent-data-roots", str(tmp_path / "missing-parent"), "--source", "missing.pt",
        "--selected", "missing.pt", "--selected-sha", "0" * 64, "--loss-weights", "missing.json",
        "--selection-config", "missing.json", "--engine-sha", "0" * 64,
        "--match-id", next(iter(case["plan"]["rounds"][-1]["manifest"]["matches"]))])
    with pytest.raises(ValueError):
        collect_selected.main()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("artifact", ["inventory", "source", "mapping", "registration"])
def test_archival_copies_exact_inputs_and_is_immutable(registration_case, tmp_path, artifact):
    case = registration_case
    output = tmp_path / "audit"
    kwargs = dict(expected_sha=semantic_digest(case["registration"]),
        inventory_paths=case["paths"], source_map_path=case["source_map"], source_paths=case["sources"], output=output)
    archive_registration(case["registration"], **kwargs)
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert len(before) == 8
    assert set(before.values()) == {json_bytes(case["registration"]), case["source_map"].read_bytes(),
        *[path.read_bytes() for path in case["paths"] + case["sources"]]}
    archive_registration(case["registration"], **kwargs)
    assert before == {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    if artifact == "source":
        path = next((output / "sources").iterdir())
    elif artifact in ("mapping", "registration"):
        path = output / ("source-map.json" if artifact == "mapping" else "registration.json")
    else:
        digest = hashlib.sha256(case["paths"][0].read_bytes()).hexdigest()
        path = output / (digest + ".json")
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="immutable artifact"):
        archive_registration(case["registration"], **kwargs)


def test_inventory_change_between_preflight_and_archive_writes_nothing(registration_case, tmp_path):
    case = registration_case
    output = tmp_path / "audit"
    validate(case)
    case["paths"][0].write_bytes(b"{}")
    with pytest.raises(ValueError, match="changed before archival"):
        archive_registration(case["registration"], expected_sha=semantic_digest(case["registration"]),
            inventory_paths=case["paths"], source_map_path=case["source_map"], source_paths=case["sources"], output=output)
    assert not output.exists()
