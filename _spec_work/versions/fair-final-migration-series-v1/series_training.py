from copy import deepcopy
import hashlib
import io
import math
from pathlib import Path

import migration_series
import torch

from migration_series import ReadOnlyMigrationStore, audit_completed_series
from native_runner.training.fair_final.checkpoint import (capture_rng, cpu_state, restore_rng,
    validate_optimizer, validate_optimizer_steps, validate_rng)
from native_runner.training.fair_final.contracts import exact_fields, integer
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.storage import durable_write
from native_runner.training.fair_migration_data import (information_from_row, teacher_targets,
    validate_teacher)
from native_runner.training.fair_migration_trainer import MigrationTrainer, resume_migration
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from native_runner.training.fair_search.storage import torch_bytes
from native_runner.training.v4.config import ModelConfigV4
from native_runner.training.v4.tensors import concatenate_padded_tensor_records


SCHEMA = "FairFinalSupplementTrainer/v1"


class SeriesMigrationDataset:
    def __init__(self, plan, data_roots, *, teacher):
        self.report = audit_completed_series(plan, data_roots)
        self.plan = deepcopy(plan)
        self.stores = tuple(ReadOnlyMigrationStore(root, manifest=entry["manifest"])
            for root, entry in zip(data_roots, plan["rounds"]))
        self.teacher = teacher
        self.index, self.receipts = {}, {}
        self.splits = {name: [] for name in migration_series.SPLITS}
        for number, store in enumerate(self.stores):
            validate_teacher(teacher, store.manifest["identity"])
            for match_id, task in sorted(store.manifest["matches"].items()):
                episode, receipt = store.load(match_id)
                digest = semantic_digest(receipt)
                if digest != self.report["identity"]["receipt_shas"][match_id]:
                    raise ValueError("migration receipt changed during series indexing")
                self.receipts[match_id] = digest
                for offset, row in enumerate(episode["rows"]):
                    row_id = row["row_id"]
                    if row_id in self.index:
                        raise ValueError("duplicate migration row across the series")
                    self.index[row_id] = (number, match_id, offset, task["split"])
                    self.splits[task["split"]].append(row_id)
        if any(not rows for rows in self.splits.values()):
            raise ValueError("complete historical train, validation and test games required")
        source = self.stores[0].manifest["identity"]
        dataset_sha = (semantic_digest(dict(manifest_sha=self.stores[0].dataset_sha, receipts=self.receipts))
            if len(self.stores) == 1 else
            semantic_digest(dict(series_sha=self.report["series_sha"], receipts=self.receipts)))
        self.identity = dict(source_a_sha=source["source_a_sha"],
            migration_adapter_sha=source["migration_adapter_sha"],
            dataset_sha=dataset_sha)

    def parent_identity(self):
        if len(self.stores) == 1:
            raise ValueError("initial series has no parent")
        plan = deepcopy(self.plan)
        plan["rounds"].pop()
        matches = {match for entry in plan["rounds"] for match in entry["manifest"]["matches"]}
        receipts = {match: self.receipts[match] for match in matches}
        if len(plan["rounds"]) == 1:
            return dict(self.identity, dataset_sha=semantic_digest(dict(
                manifest_sha=self.stores[0].dataset_sha, receipts=receipts)))
        audits = {match: self.report["identity"]["private_audit_shas"][match] for match in matches}
        series_sha = semantic_digest(dict(plan_sha=semantic_digest(plan), receipt_shas=receipts,
            private_audit_shas=audits))
        return dict(self.identity, dataset_sha=semantic_digest(dict(series_sha=series_sha, receipts=receipts)))

    def batch(self, row_ids, *, split="train"):
        ids = list(row_ids)
        if (split not in self.splits or not ids or len(set(ids)) != len(ids)
                or any(row not in self.index or self.index[row][3] != split for row in ids)):
            raise ValueError("migration batch crosses its explicit whole-game split")
        validate_teacher(self.teacher, self.stores[0].manifest["identity"])
        loaded, frames, legalities = {}, [], []
        for row_id in ids:
            number, match_id, offset, _ = self.index[row_id]
            if match_id not in loaded:
                episode, receipt = self.stores[number].load(match_id)
                if semantic_digest(receipt) != self.receipts[match_id]:
                    raise ValueError("migration data changed after series indexing")
                loaded[match_id] = episode
            row = loaded[match_id]["rows"][offset]
            info = information_from_row(row)
            action, wdl = teacher_targets(self.teacher, info)
            if (action != FullAction(**row["teacher_action"])
                    or not torch.allclose(wdl, row["teacher_wdl"], atol=1e-6, rtol=1e-5)):
                raise ValueError("recorded migration target differs from live frozen A")
            frames.append(info.frame)
            legalities.append(info.legality)
        return concatenate_padded_tensor_records(tuple(frames)), legalities


def implementation_sha():
    paths = [Path(__file__), Path(migration_series.__file__),
        migration_series.SOURCE / "native_runner/training/fair_migration_trainer.py"]
    return semantic_digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})


class SupplementTrainer(MigrationTrainer):
    frozen_epochs = 0
    trainable_epochs = 5

    def __init__(self, teacher, student, *, dataset, weights, seed, lineage,
            batch_size=256, micro_batch_size=1):
        exact_fields(lineage, {"parent_checkpoint_sha", "parent_dataset_sha", "parent_network_sha",
            "parent_optimizer_steps", "round", "series_sha"}, "supplement lineage")
        for key in ("parent_checkpoint_sha", "parent_dataset_sha", "parent_network_sha", "series_sha"):
            require_sha(lineage[key])
        integer(lineage["parent_optimizer_steps"], 0, "parent optimizer steps")
        integer(lineage["round"], 1, "supplement round")
        if (lineage["round"] not in (1, 2) or lineage["round"] != len(dataset.stores) - 1
                or lineage["series_sha"] != dataset.report["series_sha"]
                or lineage["parent_dataset_sha"] != dataset.parent_identity()["dataset_sha"]
                or any(lineage["parent_checkpoint_sha"] not in task["behavior_checkpoint_shas"]
                    for task in dataset.stores[-1].manifest["matches"].values())):
            raise ValueError("supplement lineage differs from completed series")
        super().__init__(teacher, student, identity=dataset.identity, splits=dataset.splits,
            weights=weights, seed=seed, batch_size=batch_size, micro_batch_size=micro_batch_size)
        self.lineage = deepcopy(lineage)
        self.optimizer_steps = lineage["parent_optimizer_steps"]

    @classmethod
    def from_parent(cls, path, *, expected_sha, parent_dataset, dataset, weights, seed,
            bundle=None, device="cpu", batch_size=256, micro_batch_size=1):
        original_rng = capture_rng()
        try:
            if len(dataset.stores) != len(parent_dataset.stores) + 1:
                raise ValueError("exact next supplementary round required")
            if any(left.dataset_sha != right.dataset_sha
                    for left, right in zip(dataset.stores, parent_dataset.stores)):
                raise ValueError("parent series manifest prefix differs")
            if any(dataset.receipts.get(match) != digest for match, digest in parent_dataset.receipts.items()):
                raise ValueError("parent series receipt prefix differs")
            current = dataset.stores[-1].manifest
            source = current["identity"]["source_a_sha"]
            if any(expected_sha not in task["behavior_checkpoint_shas"]
                    or task["behavior_checkpoint_shas"].count(source) != 1
                    for task in current["matches"].values()):
                raise ValueError("supplement behavior does not identify parent checkpoint")
            if len(parent_dataset.stores) == 1:
                parent = resume_migration(path, expected_sha=expected_sha, teacher=dataset.teacher,
                    identity=parent_dataset.identity, splits=parent_dataset.splits,
                    weights=weights, bundle=bundle, device=device)
            else:
                parent = resume_supplement(path, expected_sha=expected_sha, dataset=parent_dataset,
                    weights=weights, bundle=bundle, device=device)
            if parent.stage != "COMPLETE":
                raise ValueError("completed parent migration schedule required")
            lineage = dict(parent_checkpoint_sha=expected_sha,
                parent_dataset_sha=parent.identity["dataset_sha"],
                parent_network_sha=semantic_digest(parent.student.state_dict()),
                parent_optimizer_steps=parent.optimizer_steps, round=current["round"],
                series_sha=dataset.report["series_sha"])
            trainer = cls(dataset.teacher, parent.student, dataset=dataset, weights=weights,
                seed=seed, lineage=lineage, batch_size=batch_size, micro_batch_size=micro_batch_size)
            trainer.optimizer.load_state_dict(parent.optimizer.state_dict())
            validate_optimizer(trainer.optimizer, trainer.student)
            validate_optimizer_steps(trainer.optimizer, trainer.optimizer_steps)
            return trainer
        except Exception:
            restore_rng(original_rng)
            raise

    def save(self, path):
        if self.state != "RUNNING" or semantic_digest(self.teacher.state_dict()) != self.teacher_network_sha:
            raise ValueError("failed supplement or changed teacher cannot be saved")
        validate_optimizer(self.optimizer, self.student)
        validate_optimizer_steps(self.optimizer, self.optimizer_steps)
        payload = cpu_state(dict(schema=SCHEMA, implementation_sha=implementation_sha(),
            identity=self.identity, splits=self.splits, lineage=self.lineage,
            teacher_network_sha=self.teacher_network_sha, architecture=self.student.architecture,
            weights=vars(self.weights), network=self.student.state_dict(), optimizer=self.optimizer.state_dict(),
            rng=capture_rng(), cursor=dict(seed=self.seed, batch_size=self.batch_size,
                micro_batch_size=self.micro_batch_size, order=self.order, rng=self.rng.getstate(),
                optimizer_steps=self.optimizer_steps, completed_epochs=self.completed_epochs,
                offset=self.offset, consumed=self.consumed, frozen_epochs=self.frozen_epochs,
                trainable_epochs=self.trainable_epochs)))
        data = torch_bytes(dict(schema=SCHEMA, sha256=semantic_digest(payload), payload=payload))
        durable_write(path, data)
        return hashlib.sha256(data).hexdigest()


def resume_supplement(path, *, expected_sha, dataset, weights, bundle=None, device="cpu"):
    original_rng = capture_rng()
    try:
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != require_sha(expected_sha):
            raise ValueError("supplement checkpoint file checksum differs")
        envelope = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
        exact_fields(envelope, {"schema", "sha256", "payload"}, "supplement checkpoint envelope")
        payload = envelope["payload"]
        if envelope["schema"] != SCHEMA or semantic_digest(payload) != envelope["sha256"]:
            raise ValueError("supplement checkpoint semantic checksum differs")
        exact_fields(payload, {"schema", "implementation_sha", "identity", "splits", "lineage",
            "teacher_network_sha", "architecture", "weights", "network", "optimizer", "rng", "cursor"},
            "supplement checkpoint")
        if (payload["schema"] != SCHEMA or payload["implementation_sha"] != implementation_sha()
                or payload["identity"] != dataset.identity or payload["splits"] != dataset.splits
                or payload["weights"] != vars(weights)
                or payload["teacher_network_sha"] != semantic_digest(dataset.teacher.state_dict())):
            raise ValueError("supplement implementation, teacher, data, splits or loss identity differs")
        validate_rng(payload["rng"])
        cursor = payload["cursor"]
        exact_fields(cursor, {"seed", "batch_size", "micro_batch_size", "order", "rng", "optimizer_steps",
            "completed_epochs", "offset", "consumed", "frozen_epochs", "trainable_epochs"}, "supplement cursor")
        for name in ("optimizer_steps", "completed_epochs", "offset", "consumed"):
            integer(cursor[name], 0, name)
        student = ConditionalPolicyValue(bundle=bundle,
            encoder_config=ModelConfigV4(**payload["architecture"]["encoder"]))
        if student.architecture != payload["architecture"]:
            raise ValueError("supplement architecture or catalogs differ")
        current = student.state_dict()
        if set(current) != set(payload["network"]) or any(current[name].shape != value.shape
                or current[name].dtype != value.dtype for name, value in payload["network"].items()):
            raise ValueError("supplement parameter shape or dtype differs")
        student.load_state_dict(payload["network"], strict=True)
        student.to(device)
        trainer = SupplementTrainer(dataset.teacher, student, dataset=dataset, weights=weights,
            seed=cursor["seed"], batch_size=cursor["batch_size"], micro_batch_size=cursor["micro_batch_size"],
            lineage=payload["lineage"])
        local_steps = cursor["optimizer_steps"] - trainer.lineage["parent_optimizer_steps"]
        integer(local_steps, 0, "local supplement steps")
        size = len(trainer.order)
        epochs, in_epoch = divmod(local_steps, math.ceil(size / trainer.batch_size))
        if (cursor["frozen_epochs"] != 0 or cursor["trainable_epochs"] != 5
                or epochs != cursor["completed_epochs"] or epochs > 5 or (epochs == 5 and in_epoch)
                or cursor["offset"] != in_epoch * trainer.batch_size
                or cursor["consumed"] != epochs * size + cursor["offset"]):
            raise ValueError("supplement epoch and sample accounting differs")
        for epoch in range(1, min(epochs + 1, trainer.max_epochs)):
            trainer.rng.shuffle(trainer.order)
        if trainer.order != cursor["order"] or trainer.rng.getstate() != cursor["rng"]:
            raise ValueError("supplement row order or sampler RNG differs")
        trainer.optimizer.load_state_dict(payload["optimizer"])
        group = trainer.optimizer.param_groups[0]
        if group["lr"] != 1e-4 or group["weight_decay"] != 1e-4:
            raise ValueError("supplement AdamW baseline differs")
        validate_optimizer(trainer.optimizer, student)
        validate_optimizer_steps(trainer.optimizer, cursor["optimizer_steps"])
        for name in ("optimizer_steps", "completed_epochs", "offset", "consumed"):
            setattr(trainer, name, cursor[name])
        trainer._configure_stage()
        cpu_state(payload)
        restore_rng(payload["rng"])
        return trainer
    except Exception:
        restore_rng(original_rng)
        raise
