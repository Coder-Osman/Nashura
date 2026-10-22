import argparse
from dataclasses import asdict
import json
import hashlib
from pathlib import Path

import torch

from round_selection import (MigrationRound, SelectionConfig, implementation_sha, resume_round,
    selected_parent, write_artifact)
from series_training import SeriesMigrationDataset
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.storage import json_bytes
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_trainer import MigrationTrainer
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engineering", action="store_true", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--private-plan", type=Path, required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--selection-config", type=Path, required=True)
    parser.add_argument("--loss-weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--micro-batch-size", type=int, default=1)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--max-updates", type=int)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--resume-sha")
    source.add_argument("--parent", type=Path)
    parser.add_argument("--parent-sha")
    parser.add_argument("--parent-config", type=Path)
    args = parser.parse_args()
    if args.max_updates is not None and args.max_updates < 0:
        parser.error("max-updates must be nonnegative")
    if bool(args.parent) != bool(args.parent_sha) or bool(args.parent) != bool(args.parent_config):
        parser.error("parent path, SHA and selection config must be provided together")
    if args.output.exists() and not args.resume_sha:
        parser.error("existing round requires its explicit resume SHA")
    torch.set_num_threads(1)
    plan = json.loads(args.private_plan.read_text())
    teacher, _, _ = load_checkpoint(args.source, device=args.device,
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    dataset = SeriesMigrationDataset(plan, args.data_roots, teacher=teacher)
    config = SelectionConfig(**json.loads(args.selection_config.read_text()))
    weights = MigrationLossWeights(**json.loads(args.loss_weights.read_text()))
    if args.resume_sha:
        run = resume_round(args.output, expected_sha=args.resume_sha, dataset=dataset,
            weights=weights, config=config, device=args.device)
        for name in ("seed", "batch_size", "micro_batch_size"):
            if getattr(run.trainer, name) != getattr(args, name):
                parser.error("resume " + name + " differs from checkpoint")
    else:
        torch.manual_seed(args.seed)
        if args.parent:
            parent_plan = dict(plan, rounds=plan["rounds"][:-1])
            parent_dataset = SeriesMigrationDataset(parent_plan, args.data_roots[:-1], teacher=teacher)
            trainer = selected_parent(args.parent, expected_sha=args.parent_sha,
                parent_dataset=parent_dataset, dataset=dataset, weights=weights,
                config=SelectionConfig(**json.loads(args.parent_config.read_text())), seed=args.seed,
                device=args.device, batch_size=args.batch_size, micro_batch_size=args.micro_batch_size)
        else:
            if len(dataset.stores) != 1:
                parser.error("supplementary rounds require a selected parent")
            student = ConditionalPolicyValue(encoder_config=teacher.policy.backbone.config).to(args.device)
            student.copy_encoder(teacher)
            trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
                weights=weights, seed=args.seed, batch_size=args.batch_size, micro_batch_size=args.micro_batch_size)
        run = MigrationRound(trainer, dataset, config=config)
    digest = run.save(args.output, expected_previous_sha=args.resume_sha)
    updates = 0
    while run.terminal is None and (args.max_updates is None or updates < args.max_updates):
        metrics = run.advance(dataset)
        digest = run.save(args.output, expected_previous_sha=digest)
        updates += 1
        print(json.dumps(dict(event="updated", checkpoint_sha=digest,
            optimizer_steps=run.trainer.optimizer_steps, completed_epochs=run.trainer.completed_epochs,
            metrics=metrics, terminal=run.terminal)), flush=True)
    report = dict(schema="FairFinalMigrationSelectionReport/v1", checkpoint_sha=digest,
        implementation_sha=implementation_sha(), config=asdict(config), weights=vars(weights),
        dataset_identity=dataset.identity, series_sha=dataset.report["series_sha"],
        validation_sha=run.validation_sha, terminal=run.terminal,
        latest_optimizer_steps=run.trainer.optimizer_steps,
        selected=run.history[run.best_index], history=run.history,
        network_sha=semantic_digest(run.best["payload"]["network"]),
        training_admissible=False, formal_g2_pass=False)
    report_path = args.output.with_suffix(".report.json")
    previous = hashlib.sha256(report_path.read_bytes()).hexdigest() if report_path.exists() else None
    write_artifact(report_path, json_bytes(report), expected_previous_sha=previous)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
