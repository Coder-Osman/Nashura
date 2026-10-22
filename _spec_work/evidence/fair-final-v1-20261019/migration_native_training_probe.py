import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import torch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root / "project"))
    from native_runner.training.fair_final.checkpoint import capture_rng
    from native_runner.training.fair_final.episode import atomic_json
    from native_runner.training.fair_final.model import ConditionalPolicyValue
    from native_runner.training.fair_migration import MigrationLossWeights
    from native_runner.training.fair_migration_data import MigrationDataset, MigrationStore
    from native_runner.training.fair_migration_trainer import MigrationTrainer, resume_migration
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.model import load_checkpoint

    started = time.monotonic()
    sources = json.loads((args.root / "project/source-manifest.json").read_text())
    for relative, expected in sources.items():
        if hashlib.sha256((args.root / "project" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("native training probe deployed source identity changed")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(78112301)
    manifest = json.loads((args.root / "manifest.json").read_text())
    teacher, _, _ = load_checkpoint(args.source, device="cuda:0",
        expected_sha256=manifest["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    store = MigrationStore(args.root / "data", manifest=manifest)
    dataset = MigrationDataset(store, teacher=teacher)
    student = ConditionalPolicyValue(encoder_config=teacher.policy.backbone.config).to("cuda:0")
    copied = student.copy_encoder(teacher)
    weights = MigrationLossWeights(1., .1, 1.)
    trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
        weights=weights, seed=78112302, batch_size=1, micro_batch_size=1)
    source_before = semantic_digest(teacher.state_dict())
    encoder_before = semantic_digest(student.encoder.state_dict())
    network_before = semantic_digest(student.state_dict())
    first_ids = trainer.batch_ids()
    first = trainer.update_dataset(dataset)
    after_first = semantic_digest(student.state_dict())
    if after_first == network_before:
        raise ValueError("native-frame update did not change student")
    checkpoint = args.root / "engineering-migration-update.pt"
    checkpoint_sha = trainer.save(checkpoint)
    expected_ids = trainer.batch_ids()
    second = trainer.update_dataset(dataset)
    expected = dict(network=semantic_digest(student.state_dict()), optimizer=semantic_digest(trainer.optimizer.state_dict()),
        rng=semantic_digest(capture_rng()), next_ids=trainer.batch_ids(), metrics=second,
        consumed=trainer.consumed, steps=trainer.optimizer_steps)
    restored = resume_migration(checkpoint, expected_sha=checkpoint_sha, teacher=teacher,
        identity=dataset.identity, splits=dataset.splits, weights=weights, device="cuda:0")
    if restored.batch_ids() != expected_ids:
        raise ValueError("native migration resume changed next row")
    repeated = restored.update_dataset(dataset)
    actual = dict(network=semantic_digest(restored.student.state_dict()),
        optimizer=semantic_digest(restored.optimizer.state_dict()), rng=semantic_digest(capture_rng()),
        next_ids=restored.batch_ids(), metrics=repeated, consumed=restored.consumed, steps=restored.optimizer_steps)
    if actual != expected:
        raise ValueError("native migration resume differs from uninterrupted update")
    if (semantic_digest(teacher.state_dict()) != source_before
            or semantic_digest(restored.student.encoder.state_dict()) != encoder_before):
        raise ValueError("frozen native teacher or encoder changed")
    if set(first_ids + expected_ids) & set(dataset.splits["validation"] + dataset.splits["test"]):
        raise ValueError("native migration updates crossed whole-game splits")
    report = dict(schema="fair-final-native-migration-update.v1", status="passed",
        dataset_identity=dataset.identity, split_rows={key: len(value) for key, value in dataset.splits.items()},
        batch_size=1, micro_batch_size=1, engineering_only=True, training_admissible=False,
        weights=vars(weights), copied_encoder_tensors=len(copied), first_ids=first_ids, second_ids=expected_ids,
        first_metrics=first, second_metrics=second, checkpoint_sha256=checkpoint_sha,
        before_network_sha=network_before, after_first_network_sha=after_first, expected=expected, resumed=actual,
        teacher_unchanged=True, encoder_unchanged=True, exact_resume=True, seconds=time.monotonic() - started)
    atomic_json(args.root / "native-migration-update-report.json", report)
    print(json.dumps(dict(status="passed", seconds=report["seconds"], first_loss=first["loss"],
        second_loss=second["loss"], exact_resume=True)), flush=True)


if __name__ == "__main__":
    main()
