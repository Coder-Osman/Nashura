from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

from collect_selected import prepare_selected_task
import collect_selected
from test_round_selection import CONFIG, create, finish
from test_series_training import WEIGHTS, new_trainer, series
from round_selection import MigrationRound
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_search.contracts import semantic_digest


@pytest.fixture
def registered(series, tmp_path):
    run = create(series)
    finish(run, series["initial"])
    path = tmp_path / "selected.pt"
    digest = run.save(path)
    plan = deepcopy(series["plan"])
    for task in plan["rounds"][-1]["manifest"]["matches"].values():
        task["behavior_checkpoint_shas"] = [digest if model == series["digest"] else model
            for model in task["behavior_checkpoint_shas"]]
    return dict(series=series, run=run, path=path, digest=digest, plan=plan)


def prepare(registered, *, plan=None, expected=None, roots=None, digest=None):
    series = registered["series"]
    plan = registered["plan"] if plan is None else plan
    match = next(iter(plan["rounds"][-1]["manifest"]["matches"]))
    return prepare_selected_task(plan, series["roots"][:1] if roots is None else roots,
        expected_plan_sha=semantic_digest(plan) if expected is None else expected,
        teacher=series["teacher"], selected_path=registered["path"],
        selected_sha=registered["digest"] if digest is None else digest,
        weights=WEIGHTS, config=CONFIG, match_id=match, bundle=series["bundle"])


def test_collection_uses_selected_best_and_preserves_rng_and_history(registered):
    series = registered["series"]
    before = semantic_digest(capture_rng())
    parent_files = {str(path): path.read_bytes() for path in series["roots"][0].rglob("*") if path.is_file()}
    student, manifest, private = prepare(registered)
    assert semantic_digest(student.state_dict()) == semantic_digest(registered["run"].best["payload"]["network"])
    assert not any(module.training for module in student.modules())
    assert not any(parameter.requires_grad for parameter in student.parameters())
    assert semantic_digest(capture_rng()) == before
    assert parent_files == {str(path): path.read_bytes() for path in series["roots"][0].rglob("*") if path.is_file()}
    match = next(iter(manifest["matches"]))
    for key, value in registered["plan"]["rounds"][-1]["private_tasks"][match].items():
        assert private[key] == value
    assert private["parent_dataset_sha"] == series["initial"].identity["dataset_sha"]
    assert private["preregistered_plan_sha"] == semantic_digest(registered["plan"])
    assert private["training_admissible"] is False


@pytest.mark.parametrize("damage", ["plan_sha", "checkpoint_sha", "history", "overlapping_seed", "behavior", "nonterminal"])
def test_invalid_collection_lineage_is_rejected_before_native_access(registered, damage):
    options = {}
    if damage == "plan_sha":
        options["expected"] = "0" * 64
    elif damage == "checkpoint_sha":
        options["digest"] = "0" * 64
    elif damage == "history":
        options["roots"] = []
    elif damage in ("overlapping_seed", "behavior"):
        plan = deepcopy(registered["plan"])
        if damage == "overlapping_seed":
            seed = next(iter(plan["rounds"][0]["private_tasks"].values()))["engine_seed"]
            for task in plan["rounds"][-1]["private_tasks"].values():
                task["engine_seed"] = seed
        else:
            for task in plan["rounds"][-1]["manifest"]["matches"].values():
                task["behavior_checkpoint_shas"] = ["0" * 64 if model == registered["digest"] else model
                    for model in task["behavior_checkpoint_shas"]]
        options["plan"] = plan
    else:
        run = create(registered["series"])
        path = registered["path"].with_name("incomplete.pt")
        digest = run.save(path)
        old = registered["digest"]
        registered.update(path=path, digest=digest)
        for task in registered["plan"]["rounds"][-1]["manifest"]["matches"].values():
            task["behavior_checkpoint_shas"] = [digest if model == old else model
                for model in task["behavior_checkpoint_shas"]]
    with pytest.raises(ValueError):
        prepare(registered, **options)


def test_second_supplementary_round_requires_complete_parent_history(series, tmp_path):
    run = MigrationRound(new_trainer(series), series["combined"], config=CONFIG)
    finish(run, series["combined"])
    path = tmp_path / "selected-round1.pt"
    digest = run.save(path)
    plan = deepcopy(series["plan"])
    manifest = deepcopy(plan["rounds"][-1]["manifest"])
    source = manifest["identity"]["source_a_sha"]
    manifest.update(round=2, matches={})
    private = {}
    for owner in (0, 1):
        match = semantic_digest(["selected-round2", owner])
        models = [source, source]
        models[owner] = digest
        manifest["matches"][match] = dict(pair_seed_id=semantic_digest("selected-round2-pair"),
            split="train", behavior_checkpoint_shas=models)
        private[match] = dict(engine_seed=987654321, execution_seed=987654322)
    plan["rounds"].append(dict(manifest=manifest, private_tasks=private))
    registered = dict(series=series, run=run, path=path, digest=digest, plan=plan)
    student, current, audit = prepare(registered, roots=series["roots"])
    assert current["round"] == 2
    assert audit["parent_dataset_sha"] == series["combined"].identity["dataset_sha"]
    assert semantic_digest(student.state_dict()) == semantic_digest(run.best["payload"]["network"])
    with pytest.raises(ValueError, match="complete parent history"):
        prepare(registered, roots=series["roots"][-1:])


@pytest.mark.parametrize("valid_registration", [True, False])
def test_preflight_never_creates_store_or_connects_native(registered, tmp_path, monkeypatch, capsys, valid_registration):
    plan = registered["plan"]
    match = next(iter(plan["rounds"][-1]["manifest"]["matches"]))
    options = SimpleNamespace(attempt_id=1, port=29731, wall_seconds=1200.,
        private_plan=tmp_path / "plan.json", plan_sha=semantic_digest(plan),
        seed_registration=tmp_path / "registration.json", seed_registration_sha="0" * 64,
        excluded_seed_inventories=[], seed_source_map=tmp_path / "sources.json", seed_source_artifacts=[],
        match_id=match, engine_sha="0" * 64, source=tmp_path / "teacher.pt", device="cpu",
        parent_data_roots=registered["series"]["roots"][:1], selected=registered["path"],
        selected_sha=registered["digest"], loss_weights=tmp_path / "weights.json",
        selection_config=tmp_path / "config.json", preflight_only=True, output=tmp_path / "unused-output")
    import json

    for path, value in ((options.private_plan, plan), (options.seed_registration, {}),
            (options.loss_weights, WEIGHTS.__dict__), (options.selection_config, CONFIG.__dict__)):
        path.write_text(json.dumps(value))
    monkeypatch.setattr(collect_selected, "argument_parser", lambda: SimpleNamespace(parse_args=lambda: options))

    def registration(*unused, **kwargs):
        if not valid_registration:
            raise ValueError("registration rejected")
        return {}

    calls = []

    def forbidden(*unused, **kwargs):
        raise AssertionError("preflight touched store, private archive or native transport")

    def load(*unused, **kwargs):
        calls.append("model")
        return registered["series"]["teacher"], None, None

    monkeypatch.setattr(collect_selected, "validate_registration", registration)
    monkeypatch.setattr(collect_selected, "collection_protocols", lambda unused: {})
    monkeypatch.setattr(collect_selected, "load_checkpoint", load)
    monkeypatch.setattr(collect_selected, "prepare_selected_task", lambda *unused, **kwargs: prepare(registered))
    for name in ("MigrationStore", "archive_registration", "create_search_native"):
        monkeypatch.setattr(collect_selected, name, forbidden)
    if valid_registration:
        collect_selected.main()
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "passed_preflight_only"
        assert report["selected_checkpoint_sha"] == registered["digest"]
        assert report["selected_network_sha"] == semantic_digest(registered["run"].best["payload"]["network"])
        assert calls == ["model"]
    else:
        with pytest.raises(ValueError, match="registration rejected"):
            collect_selected.main()
        assert calls == []
    assert not options.output.exists()
