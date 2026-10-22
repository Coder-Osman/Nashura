import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

import torch

from round_selection import resume_round, SelectionConfig
from series_training import SeriesMigrationDataset
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def round_state(run):
    trainer = run.trainer
    return dict(network=semantic_digest(trainer.student.state_dict()),
        optimizer=semantic_digest(trainer.optimizer.state_dict()), rng=semantic_digest(capture_rng()),
        sampler=semantic_digest(dict(order=trainer.order, rng=trainer.rng.getstate(),
            offset=trainer.offset, consumed=trainer.consumed, epochs=trainer.completed_epochs)),
        next_ids=trainer.batch_ids(), steps=trainer.optimizer_steps,
        selection=semantic_digest(dict(history=run.history, best=run.best,
            best_index=run.best_index, terminal=run.terminal)),
        teacher=semantic_digest(trainer.teacher.state_dict()))


def reference_branch(run, dataset, output):
    before = round_state(run)
    first_rows = run.trainer.batch_ids()
    first_metrics = run.advance(dataset)
    midpoint = output / "midpoint.pt"
    midpoint_sha = run.save(midpoint)
    middle = round_state(run)
    next_rows = run.trainer.batch_ids()
    next_metrics = run.advance(dataset)
    final = round_state(run)
    if before["network"] == middle["network"] or middle["network"] == final["network"]:
        raise ValueError("real training updates did not change the student")
    if before["teacher"] != middle["teacher"] or middle["teacher"] != final["teacher"]:
        raise ValueError("reference branch changed frozen source A")
    if final["steps"] != before["steps"] + 2:
        raise ValueError("reference optimizer update count differs")
    if set(first_rows + next_rows) & set(dataset.splits["validation"] + dataset.splits["test"]):
        raise ValueError("reference training crossed whole-game splits")
    final_sha = run.save(output / "reference.pt")
    return dict(before=before, midpoint_state=middle, expected=final,
        midpoint_sha256=midpoint_sha, reference_checkpoint_sha256=final_sha,
        first_rows=first_rows, next_rows=next_rows, first_metrics=first_metrics,
        next_metrics=next_metrics, training_admissible=False, formal_g2_pass=False)


def resumed_branch(run, dataset, reference, output):
    if round_state(run) != reference["midpoint_state"]:
        raise ValueError("fresh-process midpoint state differs")
    if run.trainer.batch_ids() != reference["next_rows"]:
        raise ValueError("resumed next batch differs")
    metrics = run.advance(dataset)
    actual = round_state(run)
    if actual != reference["expected"] or metrics != reference["next_metrics"]:
        raise ValueError("resumed training differs from uninterrupted reference")
    checkpoint_sha = run.save(output / "resumed.pt")
    return dict(status="passed_limited_scope", actual=actual, metrics=metrics,
        resumed_checkpoint_sha256=checkpoint_sha, exact_model_optimizer_rng_sampler_selection=True,
        training_admissible=False, formal_g2_pass=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--checkpoint-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("reference", "resumed"), required=True)
    parser.add_argument("--reference-sha")
    args = parser.parse_args()
    started = time.monotonic()
    if args.mode == "resumed" and not args.reference_sha:
        parser.error("fresh-process resume requires a pinned reference SHA")
    args.output.mkdir(parents=True, exist_ok=True)
    for name, expected in json.loads((args.root / "deployment.json").read_text()).items():
        if digest(args.root / name) != expected:
            raise ValueError("frozen training deployment changed: " + name)
    torch.set_num_threads(1)
    plan = json.loads((args.root / "private-plan.json").read_text())
    teacher, _, _ = load_checkpoint(args.source, device="cuda:0",
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    dataset = SeriesMigrationDataset(plan, [args.data_root], teacher=teacher)
    weights = MigrationLossWeights(**json.loads((args.root / "loss-weights.json").read_text()))
    config = SelectionConfig(**json.loads((args.root / "selection-config.json").read_text()))
    run = resume_round(args.checkpoint, expected_sha=args.checkpoint_sha,
        dataset=dataset, weights=weights, config=config, device="cuda:0")
    print(json.dumps(dict(event="restored", mode=args.mode, steps=run.trainer.optimizer_steps)), flush=True)
    if args.mode == "reference":
        report = reference_branch(run, dataset, args.output)
    else:
        reference_path = args.output / "reference.json"
        if digest(reference_path) != args.reference_sha:
            raise ValueError("pinned reference changed")
        reference = json.loads(reference_path.read_text())
        if args.checkpoint_sha != reference["midpoint_sha256"]:
            raise ValueError("resume checkpoint differs from reference midpoint")
        report = resumed_branch(run, dataset, reference, args.output)
    report.update(mode=args.mode, seconds=time.monotonic()-started,
        input_checkpoint_sha256=args.checkpoint_sha, dataset_identity=dataset.identity,
        probe_sha256=digest(__file__))
    durable_write(args.output / (args.mode + ".json"), json_bytes(report))
    print(json.dumps(dict(event="completed", mode=args.mode, seconds=report["seconds"])), flush=True)


if __name__ == "__main__":
    main()
