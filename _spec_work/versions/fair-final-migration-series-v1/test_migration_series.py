from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from migration_series import (SCHEMA, ReadOnlyMigrationStore, audit_completed_series, main,
    validate_series)
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_final.adapter import ADAPTER_ID
from native_runner.training.fair_migration_data import DATASET_SCHEMA, IDENTITY_FIELDS
from tools.fair_migration_supplement import validate_supplement_round


EVIDENCE = Path(__file__).resolve().parents[2] / "evidence/fair-final-v1-20261019"
INITIAL = EVIDENCE / "native-migration-1792548905478386000"
SUPPLEMENT = EVIDENCE / "native-migration-supplement-1792556420626023000"
DATA_ROOTS = (INITIAL / "evidence/data", SUPPLEMENT / "data")


def fixture_plan():
    identity = {key: semantic_digest(["fixture", key]) for key in IDENTITY_FIELDS}
    identity["migration_adapter_sha"] = semantic_digest(ADAPTER_ID)
    template = dict(schema=DATASET_SCHEMA,
        namespace_sha=semantic_digest("migration-series-fixture"), identity=identity,
        card_ids=list(range(1, 9)), round=0, matches={})
    source = template["identity"]["source_a_sha"]
    rounds = []
    for number in range(3):
        manifest = deepcopy(template)
        manifest.update(round=number, matches={})
        private = {}
        for split_index, split in enumerate(("train", "validation", "test")):
            pair = semantic_digest([number, split, "pair"])
            for owner in range(2 if number else 1):
                match = semantic_digest([number, split, owner])
                models = [source, source]
                if number:
                    models[owner] = semantic_digest(["student", number])
                manifest["matches"][match] = dict(pair_seed_id=pair, split=split,
                    behavior_checkpoint_shas=models)
                private[match] = dict(engine_seed=1000 + number * 10 + split_index,
                    execution_seed=2000 + number * 10 + split_index)
        rounds.append(dict(manifest=manifest, private_tasks=private))
    return dict(schema=SCHEMA, rounds=rounds, excluded_engine_seeds=[7000])


def native_plan():
    rounds = []
    for root in DATA_ROOTS:
        manifest = json.loads((root / "dataset.json").read_text())
        private = {}
        for match_id in manifest["matches"]:
            receipt = json.loads((root / "completed" / (match_id + ".json")).read_text())
            audit = root / "audit" / match_id / str(receipt["attempt_id"]) / "private-inputs.json"
            seeds = json.loads(audit.read_text())
            private[match_id] = {key: seeds[key] for key in ("engine_seed", "execution_seed")}
        rounds.append(dict(manifest=manifest, private_tasks=private))
    return dict(schema=SCHEMA, rounds=rounds, excluded_engine_seeds=[])


def test_full_series_keeps_splits_seat_pairs_and_input_identity():
    plan = fixture_plan()
    before = deepcopy(plan)
    report = validate_series(plan, require_final_round=True)
    assert plan == before
    assert report["matches"] == 15 and report["pairs"] == report["unique_engine_seeds"] == 9
    assert report["final_round_present"] is True
    assert report["training_admissible"] is False and report["formal_g2_pass"] is False
    assert report == validate_series(deepcopy(plan), require_final_round=True)
    assert report["rounds"][2]["splits"] == dict(train=2, validation=2, test=2)


@pytest.mark.parametrize("overlap", ["match", "pair"])
def test_nonadjacent_round_overlap_passes_old_check_and_fails_series(overlap):
    plan = fixture_plan()
    initial, parent, current = [entry["manifest"] for entry in plan["rounds"]]
    old_match = next(iter(initial["matches"]))
    new_match = next(iter(current["matches"]))
    if overlap == "match":
        current["matches"][old_match] = current["matches"].pop(new_match)
        private = plan["rounds"][2]["private_tasks"]
        private[old_match] = private.pop(new_match)
    else:
        new_pair = current["matches"][new_match]["pair_seed_id"]
        for task in current["matches"].values():
            if task["pair_seed_id"] == new_pair:
                task["pair_seed_id"] = initial["matches"][old_match]["pair_seed_id"]
    validate_supplement_round(current, parent, semantic_digest(["student", 2]))
    with pytest.raises(ValueError, match="any earlier round"):
        validate_series(plan)


@pytest.mark.parametrize("bad", ["missing_zero", "missing_middle", "order", "duplicate_round", "four_rounds",
    "namespace", "engine", "source", "cards", "student", "same_seat", "missing_seat", "cross_split",
    "cross_round_seed", "cross_pair_seed", "changed_pair_seed", "excluded_seed", "missing_private",
    "extra_private", "private_hidden", "boolean_seed", "negative_seed", "float_seed", "execution_seed_type",
    "duplicate_excluded", "boolean_excluded", "overflow_seed", "overflow_excluded", "schema", "public_private_seed"])
def test_series_rejects_incomplete_identity_leaks_and_actual_seed_overlap(bad):
    plan = fixture_plan()
    first, second, third = plan["rounds"]
    tasks = third["manifest"]["matches"]
    match = next(iter(tasks))
    if bad == "missing_zero":
        plan["rounds"].pop(0)
    elif bad == "missing_middle":
        plan["rounds"].pop(1)
    elif bad == "order":
        plan["rounds"].reverse()
    elif bad == "duplicate_round":
        third["manifest"]["round"] = 1
    elif bad == "four_rounds":
        plan["rounds"].append(deepcopy(third))
    elif bad == "namespace":
        third["manifest"]["namespace_sha"] = "d" * 64
    elif bad in ("engine", "source"):
        key = "engine_protocol_sha" if bad == "engine" else "source_network_sha"
        third["manifest"]["identity"][key] = "d" * 64
    elif bad == "cards":
        third["manifest"]["card_ids"].reverse()
    elif bad == "student":
        tasks[match]["behavior_checkpoint_shas"][0] = "d" * 64
    elif bad == "same_seat":
        list(tasks.values())[1]["behavior_checkpoint_shas"] = list(tasks.values())[0]["behavior_checkpoint_shas"][:]
    elif bad == "missing_seat":
        tasks.pop(match)
        third["private_tasks"].pop(match)
    elif bad == "cross_split":
        list(tasks.values())[1]["split"] = "test"
    elif bad == "cross_round_seed":
        seed = next(iter(first["private_tasks"].values()))["engine_seed"]
        for item in third["private_tasks"].values():
            item["engine_seed"] = seed
    elif bad == "cross_pair_seed":
        list(third["private_tasks"].values())[2]["engine_seed"] = next(iter(third["private_tasks"].values()))["engine_seed"]
    elif bad == "changed_pair_seed":
        list(third["private_tasks"].values())[1]["engine_seed"] += 100
    elif bad == "excluded_seed":
        plan["excluded_engine_seeds"].append(next(iter(third["private_tasks"].values()))["engine_seed"])
    elif bad == "missing_private":
        third["private_tasks"].pop(match)
    elif bad == "extra_private":
        third["private_tasks"]["d" * 64] = dict(engine_seed=123, execution_seed=456)
    elif bad == "private_hidden":
        third["private_tasks"][match]["opponent_hand"] = []
    elif bad in ("boolean_seed", "negative_seed", "float_seed"):
        third["private_tasks"][match]["engine_seed"] = {"boolean_seed": True, "negative_seed": -1,
            "float_seed": 123.0}[bad]
    elif bad == "execution_seed_type":
        third["private_tasks"][match]["execution_seed"] = True
    elif bad == "duplicate_excluded":
        plan["excluded_engine_seeds"] *= 2
    elif bad == "boolean_excluded":
        plan["excluded_engine_seeds"] = [True]
    elif bad == "overflow_seed":
        third["private_tasks"][match]["engine_seed"] = 1 << 32
    elif bad == "overflow_excluded":
        plan["excluded_engine_seeds"] = [1 << 32]
    elif bad == "schema":
        plan["schema"] = "other"
    elif bad == "public_private_seed":
        third["manifest"]["engine_seed"] = 123
    with pytest.raises(ValueError):
        validate_series(plan)


def test_prefix_is_explicitly_not_final_and_no_unprovided_external_coverage_claim():
    plan = fixture_plan()
    plan["rounds"] = plan["rounds"][:2]
    plan["excluded_engine_seeds"] = []
    report = validate_series(plan)
    assert report["final_round_present"] is False
    assert report["external_overlap_scope"] == "provided_seed_set_only"
    with pytest.raises(ValueError):
        validate_series(plan, require_final_round=True)


def test_real_complete_episodes_audit_preserves_original_data():
    paths = [path for root in DATA_ROOTS for path in root.rglob("*") if path.is_file()]
    before = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths}
    report = audit_completed_series(native_plan(), DATA_ROOTS)
    assert report["matches"] == 5 and report["rows"] == 1310
    assert [entry["rows"] for entry in report["completed"]] == [1044, 266]
    assert report["training_merge_implemented"] is False
    assert report["teacher_targets_recomputed"] is False
    assert report["native_provenance_reverified"] is False
    assert report["final_round_present"] is False
    after = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths}
    assert before == after


@pytest.fixture
def copied_data(tmp_path):
    roots = []
    for number, source in enumerate(DATA_ROOTS):
        destination = tmp_path / str(number)
        destination.mkdir()
        shutil.copyfile(source / "dataset.json", destination / "dataset.json")
        for name in ("completed", "audit", "attempts"):
            shutil.copytree(source / name, destination / name)
        (destination / "episodes").symlink_to(source / "episodes", target_is_directory=True)
        roots.append(destination)
    return roots


@pytest.mark.parametrize("bad", ["root_count", "stored_manifest", "missing_receipt", "extra_receipt",
    "receipt_rows", "engine_seed", "execution_seed", "boolean_seed", "source", "student", "quarantine"])
def test_completed_series_rejects_missing_or_conflicting_successful_attempt_evidence(copied_data, bad):
    plan = native_plan()
    root = copied_data[1]
    match = next(iter(plan["rounds"][1]["manifest"]["matches"]))
    audit_path = root / "audit" / match / "1/private-inputs.json"
    receipt_path = root / "completed" / (match + ".json")
    if bad == "root_count":
        copied_data.pop()
    elif bad == "stored_manifest":
        value = json.loads((root / "dataset.json").read_text())
        value["namespace_sha"] = "d" * 64
        (root / "dataset.json").write_text(json.dumps(value))
    elif bad == "missing_receipt":
        receipt_path.unlink()
    elif bad == "extra_receipt":
        (root / "completed/extra.json").write_text("{}")
    elif bad == "receipt_rows":
        value = json.loads(receipt_path.read_text())
        value["rows"] += 1
        receipt_path.write_text(json.dumps(value))
    elif bad == "quarantine":
        (root / "attempts" / match / "1/quarantine.json").write_text("{}")
    else:
        value = json.loads(audit_path.read_text())
        key = {"engine_seed": "engine_seed", "execution_seed": "execution_seed", "boolean_seed": "engine_seed",
            "source": "source_sha256", "student": "student_checkpoint_sha256"}[bad]
        value[key] = True if bad == "boolean_seed" else (123 if "seed" in key else "d" * 64)
        audit_path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        audit_completed_series(plan, copied_data)


def test_cli_is_idempotent_and_conflicting_output_is_preserved(tmp_path, monkeypatch):
    path, output = tmp_path / "private-plan.json", tmp_path / "report.json"
    path.write_text(json.dumps(fixture_plan()))
    monkeypatch.setattr("sys.argv", ["migration_series.py", "--private-plan", str(path), "--output", str(output)])
    main()
    original = output.read_bytes()
    main()
    assert output.read_bytes() == original
    plan = json.loads(path.read_text())
    plan["excluded_engine_seeds"].append(8000)
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="immutable artifact"):
        main()
    assert output.read_bytes() == original


def test_audit_store_has_no_publish_path():
    plan = native_plan()
    store = ReadOnlyMigrationStore(DATA_ROOTS[0], manifest=plan["rounds"][0]["manifest"])
    with pytest.raises(TypeError, match="read only"):
        store.publish({})
