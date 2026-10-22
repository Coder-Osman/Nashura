from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import io
import math
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fair-final-migration-series-v1"))

import series_training
import torch

from series_training import SupplementTrainer, resume_supplement
from native_runner.training.fair_final.checkpoint import capture_rng, cpu_state, restore_rng
from native_runner.training.fair_final.contracts import exact_fields, integer
from native_runner.training.fair_final.storage import durable_write
from native_runner.training.fair_migration import migration_batch_loss
from native_runner.training.fair_migration_trainer import resume_migration
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from native_runner.training.fair_search.storage import atomic_write, torch_bytes


SCHEMA = "FairFinalMigrationSelection/v1"
METRICS = ("loss", "conditional_policy_kl", "greedy_nll", "wdl_teacher_kl")


@dataclass(frozen=True)
class SelectionConfig:
    patience: int
    min_delta: float

    def __post_init__(self):
        integer(self.patience, 1, "validation patience")
        if (type(self.min_delta) not in (int, float) or not math.isfinite(self.min_delta)
                or self.min_delta < 0):
            raise ValueError("finite nonnegative selection min_delta required")


def implementation_sha():
    return semantic_digest(dict(selection=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        entrypoint=hashlib.sha256(Path(__file__).with_name("train_round.py").read_bytes()).hexdigest(),
        series=series_training.implementation_sha()))


def write_artifact(path, data, *, expected_previous_sha=None):
    path = Path(path)
    if expected_previous_sha is None:
        durable_write(path, data)
    else:
        if hashlib.sha256(path.read_bytes()).hexdigest() != require_sha(expected_previous_sha):
            raise ValueError("previous artifact checksum differs")
        atomic_write(path, data, replace=True)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def validation_games(dataset):
    games = {}
    for row_id in dataset.splits["validation"]:
        number, match_id, _, split = dataset.index[row_id]
        if split != "validation":
            raise ValueError("validation index crosses whole-game split")
        games.setdefault((number, match_id), []).append(row_id)
    if not games:
        raise ValueError("nonempty validation games required")
    return [(match, rows) for (_, match), rows in sorted(games.items())]


def validation_identity(dataset):
    return semantic_digest(validation_games(dataset))


def evaluate_validation(trainer, dataset):
    if dataset.identity != trainer.identity or dataset.splits != trainer.splits:
        raise ValueError("validation dataset differs from trainer")
    original_rng = capture_rng()
    models = (trainer.teacher, trainer.student)
    modes = [(module, module.training) for model in models for module in model.modules()]
    network_shas = [semantic_digest(model.state_dict()) for model in models]
    if network_shas[0] != trainer.teacher_network_sha:
        raise ValueError("frozen validation teacher changed")
    try:
        for model in models:
            model.eval()
        results = []
        with torch.no_grad():
            for match_id, row_ids in validation_games(dataset):
                values = {key: [] for key in METRICS}
                for row_id in row_ids:
                    frame, legalities = dataset.batch([row_id], split="validation")
                    metrics = migration_batch_loss(trainer.teacher, trainer.student, frame, legalities,
                        weights=trainer.weights)
                    for key in METRICS:
                        values[key].append(float(metrics[key]))
                results.append(dict(match_id=match_id, rows=len(row_ids),
                    **{key: math.fsum(items) / len(items) for key, items in values.items()}))
        if network_shas != [semantic_digest(model.state_dict()) for model in models]:
            raise ValueError("validation mutated a model parameter or buffer")
        return dict(games=results, rows=sum(game["rows"] for game in results),
            **{key: math.fsum(game[key] for game in results) / len(results) for key in METRICS})
    finally:
        for module, mode in modes:
            module.training = mode
        restore_rng(original_rng)


def _capture(trainer):
    with tempfile.TemporaryDirectory(prefix="fair-round-save-") as directory:
        path = Path(directory) / "trainer.pt"
        trainer.save(path)
        return torch.load(path, weights_only=True, map_location="cpu")


def _restore(snapshot, dataset, weights, *, bundle, device):
    with tempfile.TemporaryDirectory(prefix="fair-round-load-") as directory:
        path = Path(directory) / "trainer.pt"
        data = torch_bytes(snapshot)
        path.write_bytes(data)
        options = dict(expected_sha=hashlib.sha256(data).hexdigest(), weights=weights,
            bundle=bundle, device=device)
        if len(dataset.stores) == 1:
            return resume_migration(path, teacher=dataset.teacher, identity=dataset.identity,
                splits=dataset.splits, **options)
        return resume_supplement(path, dataset=dataset, **options)


def _selection(history, config, frozen_epochs, max_epochs):
    best, stale, terminal = 0, 0, None
    for index, entry in enumerate(history):
        if index and terminal is not None:
            raise ValueError("validation history continues past stopping boundary")
        improved = index > 0 and entry["validation"]["loss"] < history[best]["validation"]["loss"] - config.min_delta
        if improved:
            best, stale = index, 0
        elif index > frozen_epochs:
            stale += 1
        if index >= max_epochs:
            terminal = "MAX_EPOCHS"
        elif index > frozen_epochs and stale >= config.patience:
            terminal = "EARLY_STOP"
    return best, stale, terminal


class MigrationRound:
    def __init__(self, trainer, dataset, *, config):
        if trainer.optimizer_steps != getattr(trainer, "lineage", {}).get("parent_optimizer_steps", 0):
            raise ValueError("new round must start at its initial optimizer cursor")
        if not isinstance(config, SelectionConfig) or trainer.completed_epochs or trainer.offset:
            raise ValueError("new round and explicit selection configuration required")
        self.trainer, self.config = trainer, config
        self.state = "RUNNING"
        self.validation_sha = validation_identity(dataset)
        self.history = []
        self.best = None
        self._validate(dataset)

    @property
    def terminal(self):
        return _selection(self.history, self.config, self.trainer.frozen_epochs, self.trainer.max_epochs)[2]

    @property
    def best_index(self):
        return _selection(self.history, self.config, self.trainer.frozen_epochs, self.trainer.max_epochs)[0]

    def _validate(self, dataset):
        metrics = evaluate_validation(self.trainer, dataset)
        self.history.append(dict(epoch=self.trainer.completed_epochs,
            optimizer_steps=self.trainer.optimizer_steps,
            network_sha=semantic_digest(self.trainer.student.state_dict()), validation=metrics))
        if self.best_index == len(self.history) - 1:
            self.best = _capture(self.trainer)

    def advance(self, dataset):
        if self.state != "RUNNING" or self.terminal is not None:
            raise ValueError("terminal migration round cannot consume more training rows")
        try:
            epoch = self.trainer.completed_epochs
            result = self.trainer.update_dataset(dataset)
            if self.trainer.completed_epochs != epoch:
                self._validate(dataset)
            return result
        except Exception:
            self.state = "FAILED"
            raise

    def save(self, path, *, expected_previous_sha=None):
        if self.state != "RUNNING":
            raise ValueError("failed selection round is quarantined")
        payload = cpu_state(dict(schema=SCHEMA, implementation_sha=implementation_sha(),
            config=asdict(self.config), validation_sha=self.validation_sha,
            latest=_capture(self.trainer), best=self.best, history=self.history,
            best_index=self.best_index, terminal=self.terminal))
        data = torch_bytes(dict(schema=SCHEMA, sha256=semantic_digest(payload), payload=payload))
        write_artifact(path, data, expected_previous_sha=expected_previous_sha)
        return hashlib.sha256(data).hexdigest()


def _validate_history(run, dataset):
    trainer, history = run.trainer, run.history
    if not isinstance(history, list) or len(history) != trainer.completed_epochs + 1:
        raise ValueError("one validation record per completed epoch plus baseline required")
    parent_steps = getattr(trainer, "lineage", {}).get("parent_optimizer_steps", 0)
    steps_per_epoch = math.ceil(len(trainer.splits["train"]) / trainer.batch_size)
    games = [(match, len(rows)) for match, rows in validation_games(dataset)]
    for index, entry in enumerate(history):
        exact_fields(entry, {"epoch", "optimizer_steps", "network_sha", "validation"}, "validation record")
        integer(entry["epoch"], 0, "validation epoch")
        integer(entry["optimizer_steps"], 0, "validation steps")
        require_sha(entry["network_sha"])
        if entry["epoch"] != index or entry["optimizer_steps"] != parent_steps + index * steps_per_epoch:
            raise ValueError("validation epoch or optimizer-step history differs")
        result = entry["validation"]
        exact_fields(result, {*METRICS, "rows", "games"}, "whole-game validation")
        if not isinstance(result["games"], list) or len(result["games"]) != len(games):
            raise ValueError("validation game count differs")
        for game, (match, rows) in zip(result["games"], games):
            exact_fields(game, {*METRICS, "rows", "match_id"}, "validation game")
            if game["match_id"] != match or game["rows"] != rows:
                raise ValueError("validation game identity or row count differs")
            if any(type(game[key]) not in (int, float) or not math.isfinite(game[key]) for key in METRICS):
                raise ValueError("nonfinite validation metric")
            expected = sum(getattr(trainer.weights, key) * game[key] for key in METRICS[1:])
            if not math.isclose(game["loss"], expected, rel_tol=1e-6, abs_tol=1e-6):
                raise ValueError("validation loss differs from frozen loss weights")
        if result["rows"] != sum(rows for _, rows in games):
            raise ValueError("validation row count differs")
        for key in METRICS:
            expected = math.fsum(game[key] for game in result["games"]) / len(games)
            if type(result[key]) not in (int, float) or not math.isfinite(result[key]) or result[key] != expected:
                raise ValueError("validation must weight whole games equally")
    if trainer.offset == 0 and history[-1]["network_sha"] != semantic_digest(trainer.student.state_dict()):
        raise ValueError("epoch validation and latest network differ")
    if run.terminal is not None and trainer.offset:
        raise ValueError("terminal migration round has unfinished epoch")


def resume_round(path, *, expected_sha, dataset, weights, config, bundle=None, device="cpu"):
    original_rng = capture_rng()
    try:
        data = Path(path).read_bytes()
        if hashlib.sha256(data).hexdigest() != require_sha(expected_sha):
            raise ValueError("selected round file checksum differs")
        envelope = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
        exact_fields(envelope, {"schema", "sha256", "payload"}, "selected round envelope")
        payload = envelope["payload"]
        exact_fields(payload, {"schema", "implementation_sha", "config", "validation_sha", "latest", "best",
            "history", "best_index", "terminal"}, "selected round")
        if (envelope["schema"] != SCHEMA or payload["schema"] != SCHEMA
                or semantic_digest(payload) != envelope["sha256"]
                or payload["implementation_sha"] != implementation_sha()
                or payload["config"] != asdict(config)
                or payload["validation_sha"] != validation_identity(dataset)):
            raise ValueError("selection schema, configuration, implementation or validation identity differs")
        run = object.__new__(MigrationRound)
        run.state = "RUNNING"
        run.config, run.validation_sha = config, payload["validation_sha"]
        run.history, run.best = payload["history"], payload["best"]
        run.trainer = _restore(payload["latest"], dataset, weights, bundle=bundle, device=device)
        _validate_history(run, dataset)
        if run.best_index != payload["best_index"] or run.terminal != payload["terminal"]:
            raise ValueError("selection or stopping decision differs from validation history")
        best = _restore(run.best, dataset, weights, bundle=bundle, device=device)
        latest_payload, best_payload = payload["latest"]["payload"], run.best["payload"]
        for key in ("seed", "batch_size", "micro_batch_size", "frozen_epochs", "trainable_epochs"):
            if best_payload["cursor"][key] != latest_payload["cursor"][key]:
                raise ValueError("best checkpoint belongs to a different training run")
        if best_payload.get("lineage") != latest_payload.get("lineage"):
            raise ValueError("best checkpoint lineage differs from latest")
        record = run.history[run.best_index]
        if (best.completed_epochs != record["epoch"] or best.offset
                or best.optimizer_steps != record["optimizer_steps"]
                or semantic_digest(best.student.state_dict()) != record["network_sha"]):
            raise ValueError("selected model and optimizer cursor differ from validation record")
        measured = evaluate_validation(best, dataset)
        if semantic_digest(measured) != semantic_digest(record["validation"]):
            raise ValueError("selected checkpoint live validation differs from stored metrics")
        restore_rng(payload["latest"]["payload"]["rng"])
        return run
    except Exception:
        restore_rng(original_rng)
        raise


def selected_parent(path, *, expected_sha, parent_dataset, dataset, weights, config, seed,
        bundle=None, device="cpu", batch_size=256, micro_batch_size=1):
    original_rng = capture_rng()
    try:
        if (len(dataset.stores) != len(parent_dataset.stores) + 1
                or dataset.parent_identity() != parent_dataset.identity
                or any(left.dataset_sha != right.dataset_sha
                    for left, right in zip(dataset.stores, parent_dataset.stores))
                or any(dataset.receipts.get(match) != digest for match, digest in parent_dataset.receipts.items())):
            raise ValueError("selected parent differs from completed series prefix")
        parent = resume_round(path, expected_sha=expected_sha, dataset=parent_dataset,
            weights=weights, config=config, bundle=bundle, device=device)
        if parent.terminal is None:
            raise ValueError("selected parent round must be terminal")
        best = _restore(parent.best, parent_dataset, weights, bundle=bundle, device=device)
        lineage = dict(parent_checkpoint_sha=expected_sha, parent_dataset_sha=best.identity["dataset_sha"],
            parent_network_sha=semantic_digest(best.student.state_dict()),
            parent_optimizer_steps=best.optimizer_steps, round=len(dataset.stores) - 1,
            series_sha=dataset.report["series_sha"])
        trainer = SupplementTrainer(dataset.teacher, best.student, dataset=dataset, weights=weights,
            seed=seed, lineage=lineage, batch_size=batch_size, micro_batch_size=micro_batch_size)
        trainer.optimizer.load_state_dict(deepcopy(best.optimizer.state_dict()))
        return trainer
    except Exception:
        restore_rng(original_rng)
        raise


def load_selected_student(path, *, expected_sha, dataset, weights, config, bundle=None, device="cpu"):
    original_rng = capture_rng()
    try:
        run = resume_round(path, expected_sha=expected_sha, dataset=dataset, weights=weights,
            config=config, bundle=bundle, device=device)
        if run.terminal is None:
            raise ValueError("only terminal selected model may drive supplementary collection")
        trainer = _restore(run.best, dataset, weights, bundle=bundle, device=device)
        return trainer.student.eval().requires_grad_(False)
    finally:
        restore_rng(original_rng)
