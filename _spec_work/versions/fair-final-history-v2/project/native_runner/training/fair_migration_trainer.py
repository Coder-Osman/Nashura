from copy import deepcopy
import hashlib
import io
import math
from pathlib import Path
import random

import torch

from .fair_final.checkpoint import (capture_rng, cpu_state, restore_rng, validate_optimizer,
    validate_optimizer_steps, validate_rng)
from .fair_final.contracts import exact_fields, integer
from .fair_final.model import ConditionalPolicyValue
from .fair_final.storage import durable_write
from .fair_migration import MigrationLossWeights, migration_batch_loss
from .fair_search.contracts import require_sha, semantic_digest
from .fair_search.storage import torch_bytes
from .v4.config import ModelConfigV4


SCHEMA = "FairFinalMigrationTrainer/v1"
IDENTITY_FIELDS = {"source_a_sha", "migration_adapter_sha", "dataset_sha"}


def validate_inputs(identity, splits):
    exact_fields(identity, IDENTITY_FIELDS, "migration identity")
    for value in identity.values():
        require_sha(value)
    exact_fields(splits, {"train", "validation", "test"}, "migration splits")
    all_rows = []
    for rows in splits.values():
        if not isinstance(rows, list) or not rows:
            raise ValueError("nonempty frozen migration row lists required")
        for row in rows:
            require_sha(row)
        all_rows.extend(rows)
    if len(all_rows) != len(set(all_rows)):
        raise ValueError("migration rows overlap within or across splits")


class MigrationTrainer:
    frozen_epochs = 2
    trainable_epochs = 10

    def __init__(self, teacher, student, *, identity, splits, weights, seed, batch_size=256, micro_batch_size=1):
        validate_inputs(identity, splits)
        integer(seed, 0, "migration seed")
        integer(batch_size, 1, "migration batch size")
        integer(micro_batch_size, 1, "migration micro batch size")
        if micro_batch_size > batch_size:
            raise ValueError("migration micro batch exceeds effective batch")
        if not isinstance(student, ConditionalPolicyValue) or not isinstance(weights, MigrationLossWeights):
            raise TypeError("conditional migration student and explicit loss weights required")
        if any(module.training for module in teacher.modules()) or any(
                parameter.requires_grad for parameter in teacher.parameters()):
            raise ValueError("frozen eval migration teacher required")
        if any(parameter.dtype != torch.float32 for model in (teacher, student) for parameter in model.parameters()):
            raise ValueError("FP32 migration models required")
        self.teacher, self.student, self.weights = teacher, student, weights
        self.identity, self.splits = deepcopy(identity), deepcopy(splits)
        self.teacher_network_sha = semantic_digest(teacher.state_dict())
        self.seed, self.batch_size = seed, batch_size
        self.micro_batch_size = micro_batch_size
        self.rng = random.Random(seed)
        self.order = list(range(len(self.splits["train"])))
        self.rng.shuffle(self.order)
        self.optimizer = torch.optim.AdamW(student.parameters(), lr=1e-4, weight_decay=1e-4)
        self.optimizer_steps = self.completed_epochs = self.offset = self.consumed = 0
        self.state = "RUNNING"
        self._configure_stage()

    @property
    def max_epochs(self):
        return self.frozen_epochs + self.trainable_epochs

    @property
    def stage(self):
        if self.completed_epochs == self.max_epochs:
            return "COMPLETE"
        return "FROZEN_ENCODER" if self.completed_epochs < self.frozen_epochs else "TRAINABLE_ENCODER"

    def _configure_stage(self):
        self.student.requires_grad_(True)
        self.student.train()
        self.student.encoder.requires_grad_(self.completed_epochs >= self.frozen_epochs)
        if self.completed_epochs < self.frozen_epochs:
            self.student.encoder.eval()

    def batch_ids(self):
        if self.state != "RUNNING":
            raise ValueError("failed migration trainer is quarantined")
        if self.stage == "COMPLETE":
            return []
        return [self.splits["train"][index] for index in self.order[self.offset:self.offset + self.batch_size]]

    def update(self, frame, legalities, *, row_ids):
        expected = self.batch_ids()
        if not expected or list(row_ids) != expected or frame.batch_size != len(expected):
            raise ValueError("migration batch differs from frozen training cursor")
        self._configure_stage()
        stage = self.stage
        self.optimizer.zero_grad(set_to_none=True)
        try:
            if len(legalities) != len(expected):
                raise ValueError("migration batch legality count differs")
            metrics = dict(loss=0., conditional_policy_kl=0., greedy_nll=0., wdl_teacher_kl=0.)
            for start in range(0, len(expected), self.micro_batch_size):
                count = min(self.micro_batch_size, len(expected) - start)
                result = migration_batch_loss(self.teacher, self.student, frame.narrow_batch(start, count),
                    legalities[start:start + count], weights=self.weights)
                scale = count / len(expected)
                (result["loss"] * scale).backward()
                for key in metrics:
                    metrics[key] += float(result[key].detach()) * scale
            norm = torch.nn.utils.clip_grad_norm_(self.student.parameters(), 1., error_if_nonfinite=True)
            self.optimizer.step()
            if any(not bool(torch.isfinite(parameter).all()) for parameter in self.student.parameters()):
                raise ValueError("nonfinite migration model after optimizer step")
        except Exception:
            self.state = "FAILED"
            raise
        finally:
            self.optimizer.zero_grad(set_to_none=True)
        self.optimizer_steps += 1
        self.consumed += len(expected)
        self.offset += len(expected)
        if self.offset == len(self.order):
            self.completed_epochs += 1
            self.offset = 0
            if self.completed_epochs < self.max_epochs:
                self.rng.shuffle(self.order)
        self._configure_stage()
        return {**metrics, "rows": len(expected), "stage": stage, "gradient_norm": float(norm)}

    def update_dataset(self, dataset):
        if (dataset.identity != self.identity or dataset.splits != self.splits
                or semantic_digest(dataset.teacher.state_dict()) != self.teacher_network_sha):
            raise ValueError("migration dataset differs from frozen trainer inputs")
        row_ids = self.batch_ids()
        frame, legalities = dataset.batch(row_ids)
        return self.update(frame, legalities, row_ids=row_ids)

    def save(self, path):
        if self.state != "RUNNING" or semantic_digest(self.teacher.state_dict()) != self.teacher_network_sha:
            raise ValueError("failed migration or changed teacher cannot be saved")
        validate_optimizer(self.optimizer, self.student)
        validate_optimizer_steps(self.optimizer, self.optimizer_steps)
        payload = cpu_state(dict(schema=SCHEMA, identity=self.identity, splits=self.splits,
            teacher_network_sha=self.teacher_network_sha, architecture=self.student.architecture,
            weights=vars(self.weights), network=self.student.state_dict(), optimizer=self.optimizer.state_dict(),
            rng=capture_rng(), cursor=dict(seed=self.seed, batch_size=self.batch_size,
                micro_batch_size=self.micro_batch_size, order=self.order,
                rng=self.rng.getstate(), optimizer_steps=self.optimizer_steps, completed_epochs=self.completed_epochs,
                offset=self.offset, consumed=self.consumed, frozen_epochs=self.frozen_epochs,
                trainable_epochs=self.trainable_epochs)))
        data = torch_bytes(dict(schema=SCHEMA, sha256=semantic_digest(payload), payload=payload))
        durable_write(path, data)
        return hashlib.sha256(data).hexdigest()


def resume_migration(path, *, expected_sha, teacher, identity, splits, weights, bundle=None, device="cpu"):
    original_rng = capture_rng()
    try:
        validate_inputs(identity, splits)
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != require_sha(expected_sha):
            raise ValueError("migration checkpoint file checksum differs")
        envelope = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
        exact_fields(envelope, {"schema", "sha256", "payload"}, "migration checkpoint envelope")
        payload = envelope["payload"]
        if envelope["schema"] != SCHEMA or semantic_digest(payload) != envelope["sha256"]:
            raise ValueError("migration checkpoint semantic checksum differs")
        exact_fields(payload, {"schema", "identity", "splits", "teacher_network_sha", "architecture", "weights",
            "network", "optimizer", "rng", "cursor"}, "migration checkpoint")
        if (payload["schema"] != SCHEMA or payload["identity"] != identity or payload["splits"] != splits
                or payload["weights"] != vars(weights)
                or payload["teacher_network_sha"] != semantic_digest(teacher.state_dict())):
            raise ValueError("migration teacher, data, splits or loss identity differs")
        validate_rng(payload["rng"])
        cursor = payload["cursor"]
        exact_fields(cursor, {"seed", "batch_size", "micro_batch_size", "order", "rng", "optimizer_steps", "completed_epochs",
            "offset", "consumed", "frozen_epochs", "trainable_epochs"}, "migration cursor")
        for name in ("optimizer_steps", "completed_epochs", "offset", "consumed"):
            integer(cursor[name], 0, name)
        student = ConditionalPolicyValue(bundle=bundle, encoder_config=ModelConfigV4(**payload["architecture"]["encoder"]))
        if student.architecture != payload["architecture"]:
            raise ValueError("migration checkpoint architecture or catalogs differ")
        current = student.state_dict()
        if set(current) != set(payload["network"]) or any(current[name].shape != tensor.shape
                or current[name].dtype != tensor.dtype for name, tensor in payload["network"].items()):
            raise ValueError("migration model parameter shape or dtype differs")
        student.load_state_dict(payload["network"], strict=True)
        student.to(device)
        trainer = MigrationTrainer(teacher, student, identity=identity, splits=splits, weights=weights,
            seed=cursor["seed"], batch_size=cursor["batch_size"], micro_batch_size=cursor["micro_batch_size"])
        size = len(trainer.order)
        epochs, in_epoch = divmod(cursor["optimizer_steps"], math.ceil(size / trainer.batch_size))
        if (cursor["frozen_epochs"] != trainer.frozen_epochs or cursor["trainable_epochs"] != trainer.trainable_epochs
                or epochs != cursor["completed_epochs"] or epochs > trainer.max_epochs
                or (epochs == trainer.max_epochs and in_epoch)
                or cursor["offset"] != in_epoch * trainer.batch_size
                or cursor["consumed"] != epochs * size + cursor["offset"]):
            raise ValueError("migration epoch and sample accounting differs")
        for epoch in range(1, min(epochs + 1, trainer.max_epochs)):
            trainer.rng.shuffle(trainer.order)
        if trainer.order != cursor["order"] or trainer.rng.getstate() != cursor["rng"]:
            raise ValueError("migration row order or sampler RNG differs")
        trainer.optimizer.load_state_dict(payload["optimizer"])
        group = trainer.optimizer.param_groups[0]
        if group["lr"] != 1e-4 or group["weight_decay"] != 1e-4:
            raise ValueError("migration AdamW baseline differs")
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
