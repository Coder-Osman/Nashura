import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def advance(run, dataset, count):
    results = []
    for _ in range(count):
        metrics = run.advance(dataset)
        results.append(metrics)
        print(json.dumps(dict(event="update", steps=run.trainer.optimizer_steps,
            completed_epochs=run.trainer.completed_epochs, metrics=metrics)), flush=True)
    return results


def reference(teacher, dataset, weights, config, output):
    import torch
    from native_runner.training.fair_final.model import ConditionalPolicyValue
    from native_runner.training.fair_migration_trainer import MigrationTrainer
    from native_runner.training.fair_search.contracts import semantic_digest
    from probe_native_resume import round_state
    from round_selection import MigrationRound

    torch.manual_seed(20261022)
    student = ConditionalPolicyValue(encoder_config=teacher.policy.backbone.config).to("cuda:0")
    mapping, inheritance = student.copy_encoder_with_audit(teacher)
    inheritance.update(student_initial_network_sha=semantic_digest(student.state_dict()),
        source_checkpoint_sha256=dataset.identity["source_a_sha"])
    (output / "inheritance.json").write_text(json.dumps(inheritance, indent=2))
    trainer = MigrationTrainer(teacher, student, identity=dataset.identity, splits=dataset.splits,
        weights=weights, seed=20261022, batch_size=256, micro_batch_size=1)
    run = MigrationRound(trainer, dataset, config=config)
    initial = round_state(run)
    baseline_encoder = semantic_digest(student.encoder.state_dict())
    report = dict(initial=initial, branches={}, updates=[])
    for stage, preparation in (("frozen", 1), ("trainable", 2)):
        report["updates"].extend(advance(run, dataset, preparation))
        expected_stage = "FROZEN_ENCODER" if stage == "frozen" else "TRAINABLE_ENCODER"
        if trainer.stage != expected_stage or len(trainer.batch_ids()) != 96:
            raise ValueError("midpoint does not match the real stage and whole-game cursor")
        midpoint_sha = run.save(output / (stage + "-midpoint.pt"))
        midpoint_state = round_state(run)
        metrics = advance(run, dataset, 2)
        expected_state = round_state(run)
        if ([item["rows"] for item in metrics] != [96, 256]
                or any(item["stage"] != expected_stage for item in metrics)):
            raise ValueError("resume branch must include a real 256-row update in its stage")
        encoder = semantic_digest(student.encoder.state_dict())
        if (encoder == baseline_encoder) != (stage == "frozen"):
            raise ValueError("encoder freeze or unfreeze did not match its declared stage")
        if expected_state["teacher"] != initial["teacher"]:
            raise ValueError("frozen teacher changed")
        report["branches"][stage] = dict(midpoint_sha256=midpoint_sha,
            midpoint_state=midpoint_state, expected=expected_state, metrics=metrics,
            reference_checkpoint_sha256=run.save(output / (stage + "-reference.pt")))
        report["updates"].extend(metrics)
    if trainer.optimizer_steps != 7:
        raise ValueError("reference did not preserve the seven-update schedule")
    return report


def resumed(teacher, dataset, weights, config, output, stage, reference_sha):
    from probe_native_resume import round_state
    from round_selection import resume_round

    reference_path = output / "reference.json"
    if digest(reference_path) != reference_sha:
        raise ValueError("pinned reference changed")
    target = json.loads(reference_path.read_text())["branches"][stage]
    run = resume_round(output / (stage + "-midpoint.pt"), expected_sha=target["midpoint_sha256"],
        dataset=dataset, weights=weights, config=config, device="cuda:0")
    if round_state(run) != target["midpoint_state"]:
        raise ValueError("fresh-process midpoint differs")
    metrics = advance(run, dataset, 2)
    actual = round_state(run)
    if actual != target["expected"] or metrics != target["metrics"]:
        raise ValueError("resumed model/optimizer/RNG/cursor/selection differs")
    return dict(status="passed_limited_scope", stage=stage, actual=actual, metrics=metrics,
        resumed_checkpoint_sha256=run.save(output / (stage + "-resumed.pt")),
        exact_model_optimizer_rng_sampler_selection=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--mode", choices=("reference", "frozen", "trainable"), required=True)
    parser.add_argument("--reference-sha")
    args = parser.parse_args()
    if args.mode != "reference" and args.reference_sha is None:
        parser.error("resume requires an externally pinned reference SHA")
    sys.path[:0] = [str(args.root / "firstlight_CR"),
        str(args.root / "_spec_work/versions/fair-final-migration-series-v1"),
        str(args.root / "_spec_work/versions/fair-final-migration-selection-v1"),
        str(args.root / "_spec_work/versions/fair-final-migration-integration-v1")]
    from deterministic_runtime import configure
    print(json.dumps(configure()), flush=True)
    import torch
    from native_runner.training.fair_migration import MigrationLossWeights
    from native_runner.training.fair_search.model import load_checkpoint
    from native_runner.training.fair_final.storage import durable_write, json_bytes
    from round_selection import SelectionConfig, implementation_sha
    from series_training import SeriesMigrationDataset

    torch.set_num_threads(1)
    for name, expected in json.loads((args.root / "deployment.json").read_text()).items():
        if digest(args.root / name) != expected:
            raise ValueError("frozen active deployment changed: " + name)
    started = time.monotonic()
    plan = json.loads((args.root / "private-plan.json").read_text())
    teacher, _, _ = load_checkpoint(args.source, device="cuda:0",
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    dataset = SeriesMigrationDataset(plan, [args.data_root], teacher=teacher)
    weights = MigrationLossWeights(**json.loads((args.root / "loss-weights.json").read_text()))
    config = SelectionConfig(**json.loads((args.root / "selection-config.json").read_text()))
    output = args.root / "active-comparison"
    output.mkdir(exist_ok=args.mode != "reference")
    report = (reference(teacher, dataset, weights, config, output) if args.mode == "reference"
        else resumed(teacher, dataset, weights, config, output, args.mode, args.reference_sha))
    report.update(mode=args.mode, seconds=time.monotonic() - started, dataset_identity=dataset.identity,
        implementation_sha=implementation_sha(), process=dict(pid=os.getpid(),
        start_ticks=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]),
        deployment_sha256=digest(args.root / "deployment.json"), training_admissible=False, formal_g2_pass=False)
    durable_write(output / (args.mode + ".json"), json_bytes(report))
    print(json.dumps(dict(event="completed", mode=args.mode, seconds=report["seconds"])), flush=True)


if __name__ == "__main__":
    main()
