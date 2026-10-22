import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import pstats
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.root / "firstlight_CR"),
        str(args.root / "_spec_work/versions/fair-final-migration-series-v1")]
    import torch
    from series_training import SeriesMigrationDataset
    from native_runner.training.fair_final.model import ConditionalPolicyValue
    from native_runner.training.fair_migration import MigrationLossWeights, migration_batch_loss
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.model import load_checkpoint

    torch.set_num_threads(1)
    torch.manual_seed(20261022)
    args.output.mkdir(exist_ok=False)
    deployment = json.loads((args.root / "deployment.json").read_text())
    for name, digest in deployment.items():
        if hashlib.sha256((args.root / name).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen deployment differs")
    plan = json.loads((args.root / "private-plan.json").read_text())
    teacher, _, _ = load_checkpoint(args.source, device="cuda:0",
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    student = ConditionalPolicyValue(encoder_config=teacher.policy.backbone.config).to("cuda:0")
    student.copy_encoder(teacher)
    student.eval().requires_grad_(False)
    before = [semantic_digest(model.state_dict()) for model in (teacher, student)]
    weights = MigrationLossWeights(**json.loads((args.root / "loss-weights.json").read_text()))
    stages = []

    def measure(name, operation):
        profiler = cProfile.Profile()
        torch.cuda.synchronize()
        started = time.perf_counter()
        result = profiler.runcall(operation)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        profiler.dump_stats(str(args.output / (name + ".prof")))
        stats = pstats.Stats(profiler)
        top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:20]
        stage = dict(stage=name, seconds=elapsed, calls=stats.total_calls, top=[
            dict(file=key[0], line=key[1], function=key[2], calls=value[1],
                self_seconds=value[2], cumulative_seconds=value[3]) for key, value in top])
        stages.append(stage)
        print(json.dumps(stage), flush=True)
        return result

    with torch.no_grad():
        dataset = measure("dataset_index", lambda: SeriesMigrationDataset(plan, [args.data_root], teacher=teacher))
        rows = dataset.splits["validation"]
        for number, offset in enumerate((0, len(rows) // 2, len(rows) - 1)):
            frame, legalities = measure("batch_" + str(number),
                lambda: dataset.batch([rows[offset]], split="validation"))
            metrics = measure("loss_" + str(number),
                lambda: migration_batch_loss(teacher, student, frame, legalities, weights=weights))
            stages[-1]["metrics"] = {key: float(value) for key, value in metrics.items()}
            stages[-1]["legal_cells"] = [int(legality.plays.any(-1).sum()) for legality in legalities]
    after = [semantic_digest(model.state_dict()) for model in (teacher, student)]
    if before != after:
        raise ValueError("read-only profile changed model parameters or buffers")
    report = dict(status="profiled_only", stages=stages, dataset_identity=dataset.identity,
        source_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"],
        models_unchanged=True, training_admissible=False, formal_g2_pass=False,
        deployment_sha256=hashlib.sha256((args.root / "deployment.json").read_bytes()).hexdigest(),
        probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (args.output / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
