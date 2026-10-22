from copy import copy, deepcopy
import hashlib
import json
import random

import pytest
import torch

from migration_series import SCHEMA as SERIES_SCHEMA
from series_training import SeriesMigrationDataset, SupplementTrainer, resume_supplement
from native_runner.tests.test_fair_migration_data import collect, context
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_data import MigrationDataset, MigrationStore
from native_runner.training.fair_migration_trainer import MigrationTrainer
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.storage import torch_bytes


WEIGHTS = MigrationLossWeights(1., .1, 1.)


def add_round(plan, roots, teacher, directory, *, parent_sha=None):
    number = len(roots)
    manifest = deepcopy(plan["rounds"][0]["manifest"])
    manifest.update(round=number, matches={})
    source = manifest["identity"]["source_a_sha"]
    for ordinal, split in enumerate(("train", "validation", "test") if not number else ("train", "train")):
        pair = semantic_digest(["series-test", number, split])
        match = semantic_digest(["series-test", number, split, ordinal])
        models = [source, source]
        if number:
            models[ordinal] = parent_sha
        manifest["matches"][match] = dict(pair_seed_id=pair, split=split, behavior_checkpoint_shas=models)
    store = MigrationStore(directory, manifest=manifest)
    private = {}
    for ordinal, (match, task) in enumerate(manifest["matches"].items()):
        single_manifest = deepcopy(manifest)
        single_manifest["matches"] = {match: deepcopy(task)}
        single_manifest["round"] = 0
        single_manifest["matches"][match]["behavior_checkpoint_shas"] = [source, source]
        single = MigrationStore(directory / ("staging-" + str(ordinal)), manifest=single_manifest)
        recorder, _ = collect(single, teacher, task["split"], preempt=True)
        episode, _ = single.load(recorder.match_id)
        episode["dataset_sha"] = store.dataset_sha
        store.publish(episode)
        seeds = dict(engine_seed=1000 + number * 10 + (ordinal if not number else 0),
            execution_seed=2000 + number * 10 + ordinal)
        private[match] = seeds
        audit = store.root / "audit" / match / "1/private-inputs.json"
        audit.parent.mkdir(parents=True)
        audit.write_text(json.dumps(dict(seeds, source_sha256=source,
            **({"student_checkpoint_sha256": parent_sha} if number else {}))))
    entry = dict(manifest=manifest, private_tasks=private)
    if number:
        plan["rounds"].append(entry)
    else:
        plan["rounds"][0] = entry
    roots.append(directory)


@pytest.fixture(scope="module")
def series(tmp_path_factory):
    directory = tmp_path_factory.mktemp("series-training")
    teacher, bundle, template = context.__wrapped__()
    plan = dict(schema=SERIES_SCHEMA, rounds=[dict(manifest=template)], excluded_engine_seeds=[])
    roots = []
    add_round(plan, roots, teacher, directory / "round0")
    initial = SeriesMigrationDataset(plan, roots, teacher=teacher)
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    parent = MigrationTrainer(teacher, student, identity=initial.identity, splits=initial.splits,
        weights=WEIGHTS, seed=921, batch_size=3)
    while parent.batch_ids():
        parent.update_dataset(initial)
    path = directory / "parent.pt"
    digest = parent.save(path)
    add_round(plan, roots, teacher, directory / "round1", parent_sha=digest)
    combined = SeriesMigrationDataset(plan, roots, teacher=teacher)
    return dict(directory=directory, teacher=teacher, bundle=bundle, plan=plan, roots=roots,
        initial=initial, combined=combined, parent=parent, path=path, digest=digest)


def new_trainer(series):
    return SupplementTrainer.from_parent(series["path"], expected_sha=series["digest"],
        parent_dataset=series["initial"], dataset=series["combined"], weights=WEIGHTS,
        seed=1722, bundle=series["bundle"], batch_size=5)


def restore(path, digest, series):
    return resume_supplement(path, expected_sha=digest, dataset=series["combined"],
        weights=WEIGHTS, bundle=series["bundle"])


def test_merge_preserves_historical_splits_recomputes_targets_and_parent_identity(series):
    initial, combined = series["initial"], series["combined"]
    legacy = MigrationDataset(initial.stores[0], teacher=series["teacher"])
    assert initial.identity == legacy.identity and initial.splits == legacy.splits
    assert combined.parent_identity() == initial.identity
    assert [len(combined.splits[split]) for split in ("train", "validation", "test")] == [12, 4, 4]
    for split in ("validation", "test"):
        assert combined.splits[split] == initial.splits[split]
    assert combined.identity != initial.identity
    ids = [combined.splits["train"][-1], initial.splits["train"][0]]
    frame, legalities = combined.batch(ids)
    assert frame.batch_size == len(legalities) == 2
    assert combined.batch(combined.splits["validation"], split="validation")[0].batch_size == 4
    for bad in (combined.splits["validation"], [ids[0], ids[0]], ["f" * 64], []):
        with pytest.raises(ValueError):
            combined.batch(bad)
    assert combined.report["training_admissible"] is False


def test_supplement_preserves_parent_weights_moments_and_bounds_complete_coverage(series):
    trainer = new_trainer(series)
    parent = series["parent"]
    assert semantic_digest(trainer.student.state_dict()) == semantic_digest(parent.student.state_dict())
    assert semantic_digest(trainer.optimizer.state_dict()) == semantic_digest(parent.optimizer.state_dict())
    teacher_sha = semantic_digest(trainer.teacher.state_dict())
    encoder_sha = semantic_digest(trainer.student.encoder.state_dict())
    seen = []
    while trainer.batch_ids():
        seen.extend(trainer.batch_ids())
        metrics = trainer.update_dataset(series["combined"])
        assert metrics["stage"] == "TRAINABLE_ENCODER"
    assert trainer.completed_epochs == 5 and trainer.stage == "COMPLETE"
    assert trainer.consumed == 60 and trainer.optimizer_steps == parent.optimizer_steps + 15
    assert all(seen.count(row) == 5 for row in series["combined"].splits["train"])
    assert not set(seen) & set(series["combined"].splits["validation"] + series["combined"].splits["test"])
    assert semantic_digest(trainer.teacher.state_dict()) == teacher_sha
    assert semantic_digest(trainer.student.encoder.state_dict()) != encoder_sha


@pytest.mark.parametrize("updates", [0, 1, 2, 3, 14, 15])
def test_exact_resume_partial_batch_epoch_and_completion(series, tmp_path, updates):
    trainer = new_trainer(series)
    for ordinal in range(updates):
        trainer.update_dataset(series["combined"])
    path = tmp_path / "supplement.pt"
    digest = trainer.save(path)
    before = semantic_digest(capture_rng())
    expected_ids = trainer.batch_ids()
    expected = trainer.update_dataset(series["combined"]) if expected_ids else None
    network = semantic_digest(trainer.student.state_dict())
    optimizer = semantic_digest(trainer.optimizer.state_dict())
    rng = semantic_digest(capture_rng())
    torch.manual_seed(9871)
    restored = restore(path, digest, series)
    assert semantic_digest(capture_rng()) == before
    assert restored.batch_ids() == expected_ids
    actual = restored.update_dataset(series["combined"]) if expected_ids else None
    assert actual == expected
    assert semantic_digest(restored.student.state_dict()) == network
    assert semantic_digest(restored.optimizer.state_dict()) == optimizer
    assert semantic_digest(capture_rng()) == rng


@pytest.mark.parametrize("bad", ["schema", "implementation", "dataset", "split", "weight", "lineage_parent",
    "lineage_dataset", "lineage_round", "lineage_series", "order", "cursor_rng", "steps", "offset", "consumed",
    "schedule", "epoch", "optimizer", "moment", "dtype", "nonfinite", "rng"])
def test_resealed_corruption_rejected_with_global_rng_unchanged(series, tmp_path, bad):
    trainer = new_trainer(series)
    trainer.update_dataset(series["combined"])
    path = tmp_path / "supplement.pt"
    trainer.save(path)
    envelope = torch.load(path, weights_only=True)
    payload = envelope["payload"]
    if bad == "schema":
        payload["schema"] = "other"
    elif bad == "implementation":
        payload["implementation_sha"] = "f" * 64
    elif bad == "dataset":
        payload["identity"]["dataset_sha"] = "f" * 64
    elif bad == "split":
        payload["splits"]["train"].reverse()
    elif bad == "weight":
        payload["weights"]["greedy_nll"] = 9.
    elif bad.startswith("lineage_"):
        key = {"lineage_parent": "parent_checkpoint_sha", "lineage_dataset": "parent_dataset_sha",
            "lineage_round": "round", "lineage_series": "series_sha"}[bad]
        payload["lineage"][key] = 2 if key == "round" else "f" * 64
    elif bad == "order":
        payload["cursor"]["order"].reverse()
    elif bad == "cursor_rng":
        payload["cursor"]["rng"] = random.Random(999).getstate()
    elif bad in ("steps", "offset", "consumed", "epoch", "schedule"):
        key = {"steps": "optimizer_steps", "offset": "offset", "consumed": "consumed",
            "epoch": "completed_epochs", "schedule": "trainable_epochs"}[bad]
        payload["cursor"][key] += 1
    elif bad == "optimizer":
        payload["optimizer"]["param_groups"][0]["lr"] = .1
    elif bad == "moment":
        next(iter(payload["optimizer"]["state"].values()))["exp_avg"] = torch.zeros(1)
    elif bad == "dtype":
        name = next(iter(payload["network"]))
        payload["network"][name] = payload["network"][name].half()
    elif bad == "nonfinite":
        next(iter(payload["network"].values())).fill_(float("nan"))
    else:
        payload["rng"]["cpu"] = torch.zeros(1, dtype=torch.uint8)
    envelope["sha256"] = semantic_digest(payload)
    data = torch_bytes(envelope)
    path.write_bytes(data)
    before = semantic_digest(capture_rng())
    with pytest.raises((ValueError, RuntimeError)):
        restore(path, hashlib.sha256(data).hexdigest(), series)
    assert semantic_digest(capture_rng()) == before


@pytest.mark.parametrize("bad", ["file", "incomplete", "foreign_parent", "skip_round", "receipt"])
def test_transition_rejects_foreign_or_unfinished_parent_and_restores_rng(series, tmp_path, bad):
    path, digest = series["path"], series["digest"]
    parent, dataset = series["initial"], series["combined"]
    if bad == "file":
        digest = "f" * 64
    elif bad == "incomplete":
        student = ConditionalPolicyValue(bundle=series["bundle"],
            encoder_config=series["teacher"].policy.backbone.config)
        student.load_state_dict(series["parent"].student.state_dict())
        trainer = MigrationTrainer(series["teacher"], student,
            identity=parent.identity, splits=parent.splits, weights=WEIGHTS, seed=2)
        path = tmp_path / "unfinished.pt"
        digest = trainer.save(path)
        dataset = copy(dataset)
        dataset.stores = tuple(copy(store) for store in dataset.stores)
        dataset.stores[-1].manifest = deepcopy(dataset.stores[-1].manifest)
        for task in dataset.stores[-1].manifest["matches"].values():
            task["behavior_checkpoint_shas"] = [digest if item != dataset.identity["source_a_sha"] else item
                for item in task["behavior_checkpoint_shas"]]
    elif bad == "foreign_parent":
        parent = copy(parent)
        parent.stores = tuple(copy(store) for store in parent.stores)
        parent.stores[0].dataset_sha = "f" * 64
    elif bad == "skip_round":
        parent = dataset
    else:
        parent = copy(parent)
        parent.receipts = dict(parent.receipts)
        match = next(iter(parent.receipts))
        parent.receipts[match] = "f" * 64
    before = semantic_digest(capture_rng())
    with pytest.raises(ValueError):
        SupplementTrainer.from_parent(path, expected_sha=digest, parent_dataset=parent,
            dataset=dataset, weights=WEIGHTS, seed=821, bundle=series["bundle"])
    assert semantic_digest(capture_rng()) == before


def test_native_seed_overlap_is_rejected_before_any_model_or_batch_access():
    from test_migration_series import DATA_ROOTS, native_plan

    plan = native_plan()
    seed = next(iter(plan["rounds"][0]["private_tasks"].values()))["engine_seed"]
    for task in plan["rounds"][1]["private_tasks"].values():
        task["engine_seed"] = seed
    with pytest.raises(ValueError, match="actual engine seed overlaps"):
        SeriesMigrationDataset(plan, DATA_ROOTS, teacher=object())


def test_batch_recomputes_teacher_and_revalidates_receipts(series, monkeypatch):
    import series_training

    dataset = series["combined"]
    ids = dataset.splits["train"][:1]
    original = series_training.teacher_targets

    def changed_target(teacher, info):
        action, wdl = original(teacher, info)
        return action, wdl + .1

    monkeypatch.setattr(series_training, "teacher_targets", changed_target)
    with pytest.raises(ValueError, match="live frozen A"):
        dataset.batch(ids)
    monkeypatch.setattr(series_training, "teacher_targets", original)
    index = dataset.index[ids[0]]
    store = dataset.stores[index[0]]
    original_load = store.load

    def changed_receipt(match):
        episode, receipt = original_load(match)
        return episode, dict(receipt, rows=receipt["rows"] + 1)

    monkeypatch.setattr(store, "load", changed_receipt)
    with pytest.raises(ValueError, match="changed after series indexing"):
        dataset.batch(ids)


def test_round_two_uses_complete_prior_series_and_optimizer(series, tmp_path):
    trainer = new_trainer(series)
    while trainer.batch_ids():
        trainer.update_dataset(series["combined"])
    path = tmp_path / "round1.pt"
    digest = trainer.save(path)
    plan, roots = deepcopy(series["plan"]), list(series["roots"])
    add_round(plan, roots, series["teacher"], tmp_path / "round2", parent_sha=digest)
    dataset = SeriesMigrationDataset(plan, roots, teacher=series["teacher"])
    child = SupplementTrainer.from_parent(path, expected_sha=digest, parent_dataset=series["combined"],
        dataset=dataset, weights=WEIGHTS, seed=941, bundle=series["bundle"], batch_size=7)
    assert dataset.parent_identity() == series["combined"].identity
    assert child.lineage["round"] == 2
    assert semantic_digest(child.optimizer.state_dict()) == semantic_digest(trainer.optimizer.state_dict())
    assert child.update_dataset(dataset)["rows"] == 7
    target = tmp_path / "round2.pt"
    sha = child.save(target)
    restored = resume_supplement(target, expected_sha=sha, dataset=dataset, weights=WEIGHTS, bundle=series["bundle"])
    assert child.batch_ids() == restored.batch_ids()
    assert semantic_digest(child.optimizer.state_dict()) == semantic_digest(restored.optimizer.state_dict())
