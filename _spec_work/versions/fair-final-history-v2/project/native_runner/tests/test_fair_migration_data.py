from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_episode import final
from native_runner.tests.test_fair_final_learning import fixture_source
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.fair_final.adapter import ADAPTER_ID
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.storage import EpisodeStore
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_data import (DATASET_SCHEMA, MigrationDataset, MigrationRecorder,
    MigrationStore, information_from_row, teacher_targets, validate_episode, validate_manifest)
from native_runner.training import fair_migration_data as data_module
from native_runner.training.fair_migration_trainer import MigrationTrainer, resume_migration
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.storage import save_episode


@pytest.fixture
def context():
    torch.manual_seed(761)
    teacher, bundle = fixture_source()
    with torch.no_grad():
        teacher.policy.gate_head[-1].weight.zero_()
        teacher.policy.gate_head[-1].bias.copy_(torch.tensor([100., -100.]))
    teacher.requires_grad_(False)
    identity = {key: semantic_digest(key) for key in data_module.IDENTITY_FIELDS}
    identity.update(source_network_sha=semantic_digest(teacher.state_dict()), migration_adapter_sha=semantic_digest(ADAPTER_ID))
    matches = {semantic_digest(name): dict(pair_seed_id=semantic_digest([name, "pair"]), split=name,
        behavior_checkpoint_shas=[identity["source_a_sha"]] * 2) for name in ("train", "validation", "test")}
    manifest = dict(schema=DATASET_SCHEMA, namespace_sha=semantic_digest("migration-data-tests"), identity=identity,
        card_ids=list(bundle.card_catalog.raw_card_ids), round=0, matches=matches)
    return teacher, bundle, manifest


def collect(store, teacher, split, *, attempt=1, preempt=False, winner=0, publish=True):
    match_id = next(key for key, task in store.manifest["matches"].items() if task["split"] == split)
    recorder = MigrationRecorder(store, match_id=match_id, attempt_id=attempt, teacher=teacher)
    clock = DecisionClock()
    clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    plays[0, 0, 1:3] = True
    legality = LegalPrefixes(store.manifest["card_ids"], plays)
    for tick in ([100, 101] if preempt else [100]):
        if tick == 101:
            clock.begin_tick(tick, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
            recorder.reconcile(clock)
        information = {owner: DecisionInformation(owner, tick, _batch(1), legality) for owner in (0, 1)}
        actions = {owner: teacher_targets(teacher, info)[0] for owner, info in information.items()}
        if store.manifest["round"]:
            actions[1] = FullAction(store.manifest["card_ids"][0], 0, 1)
        clock.commit(actions, legal=lambda *args: True)
        recorder.record_committed(information, actions, clock)
    runtime = SimpleNamespace(clock=clock, ended=True, truncated=False, failed=False, frozen={})
    terminal, raw = final(winner)
    if not publish:
        return recorder, runtime, terminal, raw
    clock.finish(102, outcomes={})
    result = recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    return recorder, result


def complete(tmp_path, context):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    for split in ("train", "validation", "test"):
        collect(store, teacher, split, preempt=True)
    return store, MigrationDataset(store, teacher=teacher)


@pytest.mark.parametrize("winner,labels", [(0, ["WIN", "LOSS"]), (1, ["LOSS", "WIN"]), (None, ["DRAW", "DRAW"])])
def test_migration_complete_roundtrip_keeps_preempted_rows_and_player_labels(tmp_path, context, winner, labels):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, result = collect(store, teacher, "train", preempt=True, winner=winner)
    episode, receipt = store.load(recorder.match_id)
    assert result["status"] == "published" and receipt["rows"] == 4
    assert [row["wdl"] for row in episode["rows"]] == labels * 2
    assert [row["outcome"]["reason"] for row in episode["rows"]] == ["preempted"] * 2 + ["terminal_expired"] * 2
    assert all(row["teacher_conditionals_ref"]["schema"] == "FrozenASourceConditionals/v1" for row in episode["rows"])


@pytest.mark.parametrize("bad", ["schema", "adapter", "pair_overlap", "round", "source_behavior", "private_seed"])
def test_migration_manifest_rejects_wrong_semantics_and_whole_game_split_leaks(context, bad):
    teacher, bundle, manifest = context
    manifest = deepcopy(manifest)
    if bad == "schema":
        manifest["schema"] = "FairFinalDataset/v1"
    elif bad == "adapter":
        manifest["identity"]["migration_adapter_sha"] = "a" * 64
    elif bad == "pair_overlap":
        tasks = list(manifest["matches"].values())
        tasks[1]["pair_seed_id"] = tasks[0]["pair_seed_id"]
    elif bad == "round":
        manifest["round"] = 3
    elif bad == "source_behavior":
        next(iter(manifest["matches"].values()))["behavior_checkpoint_shas"][1] = "b" * 64
    else:
        manifest["engine_seed"] = 123
    with pytest.raises(ValueError):
        validate_manifest(manifest)


@pytest.mark.parametrize("bad", ["truncated", "failed", "unconfirmed", "rejected", "not_finalized"])
def test_migration_failed_attempt_is_quarantined_without_completion(tmp_path, context, bad):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, runtime, terminal, raw = collect(store, teacher, "train", publish=False)
    if bad != "unconfirmed":
        runtime.clock.finish(102, outcomes={})
    if bad == "truncated":
        runtime.truncated = True
    elif bad == "failed":
        runtime.failed = True
    elif bad == "rejected":
        runtime.clock.evidence[0]["reason"] = "rejected"
    elif bad == "not_finalized":
        raw["finalized"] = False
    with pytest.raises(ValueError):
        recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    recorder.quarantine(bad)
    assert not (store.root / "completed" / f"{recorder.match_id}.json").exists()
    assert json.loads((recorder.directory / "quarantine.json").read_text())["published"] is False


@pytest.mark.parametrize("bad", ["row_id", "owner_bool", "fair_hash", "teacher_ref", "wdl", "label", "zero_delay",
    "mask_dtype", "frame_precision", "reservation", "outcome", "missing_boundary", "private_hidden"])
def test_migration_serialized_corruption_rejected(tmp_path, context, bad):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, result = collect(store, teacher, "train", preempt=True)
    episode, _ = store.load(recorder.match_id)
    row = episode["rows"][0]
    if bad == "row_id":
        row["row_id"] = "a" * 64
    elif bad == "owner_bool":
        row["owner"] = False
    elif bad == "fair_hash":
        row["fair_sha"] = "a" * 64
    elif bad == "teacher_ref":
        row["teacher_conditionals_ref"]["source_a_sha"] = "a" * 64
    elif bad == "wdl":
        row["teacher_wdl"][0] = float("nan")
    elif bad == "label":
        row["wdl"] = "DRAW"
    elif bad == "zero_delay":
        row["legality"]["plays"][0, 0, 0] = True
    elif bad == "mask_dtype":
        row["legality"]["plays"] = row["legality"]["plays"].float()
    elif bad == "frame_precision":
        row["frame"] = data_module.pack(information_from_row(row).frame.to_storage(float_dtype=torch.float16))
    elif bad == "reservation":
        row["outcome"]["reservation"] = True
    elif bad == "outcome":
        row["outcome"]["reason"] = "unconfirmed"
    elif bad == "missing_boundary":
        row["outcome"]["tick"] = 100
    else:
        row["opponent_private_hand"] = [1, 2, 3, 4]
    with pytest.raises(ValueError):
        validate_episode(episode, manifest)


def test_migration_loader_uses_complete_game_splits_and_real_model_update_resume(tmp_path, context):
    store, dataset = complete(tmp_path / "data", context)
    teacher, bundle, manifest = context
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    weights = MigrationLossWeights(1., .1, 1.)
    trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
        weights=weights, seed=722, batch_size=3)
    teacher_sha = semantic_digest(teacher.state_dict())
    before = semantic_digest(student.policy_head.state_dict())
    result = trainer.update_dataset(dataset)
    assert result["rows"] == 3 and before != semantic_digest(student.policy_head.state_dict())
    path = tmp_path / "migration.pt"
    sha = trainer.save(path)
    restored = resume_migration(path, expected_sha=sha, teacher=teacher, identity=dataset.identity,
        splits=dataset.splits, weights=weights, bundle=bundle)
    assert restored.batch_ids() == trainer.batch_ids()
    assert restored.update_dataset(dataset) == trainer.update_dataset(dataset)
    assert semantic_digest(restored.student.state_dict()) == semantic_digest(trainer.student.state_dict())
    assert semantic_digest(teacher.state_dict()) == teacher_sha


def test_validation_and_test_rows_are_blocked_from_training_loader(tmp_path, context):
    store, dataset = complete(tmp_path, context)
    for split in ("validation", "test"):
        with pytest.raises(ValueError, match="whole-game split"):
            dataset.batch(dataset.splits[split])
        frame, legalities = dataset.batch(dataset.splits[split], split=split)
        assert frame.batch_size == len(legalities) == 4
    with pytest.raises(ValueError):
        dataset.batch([dataset.splits["train"][0]] * 2)


def test_dataset_missing_one_registered_game_is_not_ready(tmp_path, context):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    collect(store, teacher, "train")
    with pytest.raises(FileNotFoundError):
        MigrationDataset(store, teacher=teacher)


def test_duplicate_attempt_is_idempotent_and_dataset_identity_binds_actual_content(tmp_path, context):
    store, dataset = complete(tmp_path, context)
    teacher, bundle, manifest = context
    recorder, result = collect(store, teacher, "train", attempt=2, winner=1)
    assert result["status"] == "duplicate"
    assert MigrationDataset(store, teacher=teacher).identity == dataset.identity
    episode, receipt = store.load(recorder.match_id)
    assert episode["attempt_id"] == 1
    assert len(list((store.root / "completed").glob("*.json"))) == 3


def test_same_manifest_with_different_completed_results_has_different_dataset_identity(tmp_path, context):
    store, dataset = complete(tmp_path / "first", context)
    teacher, bundle, manifest = context
    other = MigrationStore(tmp_path / "second", manifest=manifest)
    for split in ("train", "validation", "test"):
        collect(other, teacher, split, preempt=True, winner=1)
    assert other.dataset_sha == store.dataset_sha
    assert MigrationDataset(other, teacher=teacher).identity != dataset.identity


@pytest.mark.parametrize("bad", ["file", "rows", "attempt", "bool_attempt", "quarantine"])
def test_store_rejects_corrupted_payload_or_completion_receipt(tmp_path, context, bad):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, result = collect(store, teacher, "train")
    path = store.root / "completed" / f"{recorder.match_id}.json"
    receipt = json.loads(path.read_text())
    if bad == "file":
        (store.root / "episodes" / f"{receipt['semantic_sha']}.pt.gz").write_bytes(b"corrupted payload")
    elif bad == "quarantine":
        (recorder.directory / "quarantine.json").write_text('{"published":false}')
    else:
        field = "rows" if bad == "rows" else "attempt_id"
        receipt[field] = True if bad == "bool_attempt" else receipt[field] + 1
        path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        store.load(recorder.match_id)


def test_valid_receipt_replacement_after_indexing_is_rejected(tmp_path, context):
    store, dataset = complete(tmp_path / "first", context)
    teacher, bundle, manifest = context
    other = MigrationStore(tmp_path / "replacement", manifest=manifest)
    collect(other, teacher, "train", winner=1)
    match_id = semantic_digest("train")
    episode, receipt = other.load(match_id)
    original_load = store.load
    store.load = lambda selected: (episode, receipt) if selected == match_id else original_load(selected)
    with pytest.raises(ValueError, match="changed after indexing"):
        dataset.batch([dataset.splits["train"][0]])


@pytest.mark.parametrize("bad", ["identity", "split", "teacher"])
def test_dataset_mismatch_is_rejected_before_optimizer_or_cursor_changes(tmp_path, context, bad):
    store, dataset = complete(tmp_path, context)
    teacher, bundle, manifest = context
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=teacher.policy.backbone.config)
    trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
        weights=MigrationLossWeights(1., .1, 1.), seed=722, batch_size=3)
    if bad == "identity":
        dataset.identity["dataset_sha"] = "d" * 64
    elif bad == "split":
        dataset.splits["train"].reverse()
    else:
        with torch.no_grad():
            teacher.wdl_head[-1].bias.add_(1.)
    before = semantic_digest(dict(network=student.state_dict(), optimizer=trainer.optimizer.state_dict(),
        batch=trainer.batch_ids(), rng=capture_rng()))
    with pytest.raises(ValueError, match="frozen trainer inputs"):
        trainer.update_dataset(dataset)
    assert before == semantic_digest(dict(network=student.state_dict(), optimizer=trainer.optimizer.state_dict(),
        batch=trainer.batch_ids(), rng=capture_rng()))


def test_interrupted_completion_can_retry_without_partial_admission(tmp_path, context, monkeypatch):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, runtime, terminal, raw = collect(store, teacher, "train", publish=False)
    runtime.clock.finish(102, outcomes={})
    original = data_module.durable_write

    def interrupted(path, data):
        if Path(path).parent.name == "completed":
            raise OSError("injected receipt interruption")
        return original(path, data)

    monkeypatch.setattr(data_module, "durable_write", interrupted)
    with pytest.raises(OSError, match="interruption"):
        recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    assert not (store.root / "completed" / f"{recorder.match_id}.json").exists()
    monkeypatch.setattr(data_module, "durable_write", original)
    assert recorder.publish(terminal=terminal, raw=raw, runtime=runtime)["status"] == "published"


def test_soft_targets_recomputed_from_live_teacher_and_forged_values_rejected(tmp_path, context):
    store, dataset = complete(tmp_path, context)
    match_id = semantic_digest("train")
    episode, receipt = store.load(match_id)
    episode["rows"][0]["teacher_wdl"] = torch.tensor([1., 0., 0.])
    validate_episode(episode, store.manifest)
    dataset.store.load = lambda selected: (episode, receipt) if selected == match_id else None
    with pytest.raises(ValueError, match="live frozen A"):
        dataset.batch([dataset.splits["train"][0]])


def test_teacher_mutation_and_manifest_drift_fail_closed(tmp_path, context):
    store, dataset = complete(tmp_path, context)
    teacher, bundle, manifest = context
    with torch.no_grad():
        teacher.wdl_head[-1].bias.add_(1.)
    with pytest.raises(ValueError, match="unchanged frozen"):
        dataset.batch(dataset.splits["train"])
    store.manifest["namespace_sha"] = "d" * 64
    with pytest.raises(ValueError, match="manifest changed"):
        store.load(semantic_digest("train"))


def test_student_supplement_keeps_behavior_separate_from_teacher(tmp_path, context):
    teacher, bundle, manifest = context
    manifest["round"] = 1
    for task in manifest["matches"].values():
        task["behavior_checkpoint_shas"][1] = "e" * 64
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, runtime, terminal, raw = collect(store, teacher, "train", publish=False)
    runtime.clock.finish(102, outcomes={1: "executed"})
    recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    episode, _ = store.load(recorder.match_id)
    row = episode["rows"][1]
    assert row["behavior_action"] != row["teacher_action"]
    assert row["behavior_action"]["card_id"] is not None and row["teacher_action"]["card_id"] is None


def test_direct_migration_data_is_not_search_or_formal_replay(tmp_path, context):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder, result = collect(store, teacher, "train")
    episode, _ = store.load(recorder.match_id)
    with pytest.raises(TypeError, match="SearchEpisode"):
        save_episode(tmp_path / "search", episode)
    formal = object.__new__(EpisodeStore)
    with pytest.raises(TypeError, match="AZJointEpisode"):
        formal._admit(episode)


def test_recording_is_atomic_for_both_frozen_sides_and_keeps_global_rng(tmp_path, context):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    recorder = MigrationRecorder(store, match_id=semantic_digest("train"), attempt_id=1, teacher=teacher)
    clock = DecisionClock()
    clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    with pytest.raises(ValueError):
        clock.commit({0: FullAction()}, legal=lambda *args: True)
    assert not recorder.rows
    before = semantic_digest(capture_rng())
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    info = DecisionInformation(0, 100, _batch(1), LegalPrefixes(manifest["card_ids"], plays))
    target, wdl = teacher_targets(teacher, info)
    assert target == FullAction() and wdl.shape == (3,)
    assert semantic_digest(capture_rng()) == before
