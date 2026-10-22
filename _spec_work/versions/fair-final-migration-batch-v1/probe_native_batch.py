import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time


def numeric_metrics(metrics):
    return {key: float(value.detach() if hasattr(value, "detach") else value)
        for key, value in metrics.items()}


def check_parameter_update(baseline, batched, *, name, output, context):
    import torch
    try:
        torch.testing.assert_close(baseline, batched, atol=3e-6, rtol=2e-5, msg=name)
    except AssertionError:
        difference = (baseline.detach() - batched.detach()).abs()
        tolerance = 3e-6 + 2e-5 * batched.detach().abs()
        failing = difference > tolerance
        indices = torch.nonzero(failing.flatten()).flatten()
        selected = indices[torch.argsort(difference.flatten()[indices], descending=True)[:12]]
        details = dict(parameter=name, shape=list(baseline.shape), count=int(failing.sum()),
            maximum_absolute_delta=float(difference.max()), context=context,
            flat_indices=selected.cpu().tolist(),
            baseline=baseline.detach().flatten()[selected].cpu().tolist(),
            batched=batched.detach().flatten()[selected].cpu().tolist(),
            baseline_gradient=baseline.grad.flatten()[selected].cpu().tolist(),
            batched_gradient=batched.grad.flatten()[selected].cpu().tolist())
        Path(output).write_text(json.dumps(details, indent=2))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.root / "firstlight_CR"),
        str(args.root / "_spec_work/versions/fair-final-migration-series-v1")]
    from deterministic_runtime import configure
    print(json.dumps(configure()), flush=True)
    import torch
    from series_training import SeriesMigrationDataset
    from native_runner.training.fair_final.checkpoint import capture_rng, restore_rng
    from native_runner.training.fair_final.model import ConditionalPolicyValue
    from native_runner.training import fair_migration as optimized
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.model import load_checkpoint
    torch.set_num_threads(1)
    for name, digest in json.loads((args.root / "deployment.json").read_text()).items():
        if hashlib.sha256((args.root / name).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen deployment changed")
    args.output.mkdir(exist_ok=False)
    spec = importlib.util.spec_from_file_location("native_runner.training.batch_reference",
        args.root / "reference_migration.py")
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)
    plan = json.loads((args.root / "private-plan.json").read_text())
    teacher, _, _ = load_checkpoint(args.source, device="cuda:0",
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    teacher_sha = semantic_digest(teacher.state_dict())
    dataset = SeriesMigrationDataset(plan, [args.data_root], teacher=teacher)
    rows = dataset.splits["validation"]
    selected = [rows[0], rows[len(rows) // 2], rows[-1]]
    weights = json.loads((args.root / "loss-weights.json").read_text())
    timing = []
    comparisons = []

    def timed(name, operation):
        torch.cuda.synchronize()
        started = time.perf_counter()
        result = operation()
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        timing.append(dict(implementation=name, seconds=elapsed))
        return result

    for frozen in (True, False):
        torch.manual_seed(20261022)
        students = [ConditionalPolicyValue(encoder_config=teacher.policy.backbone.config).to("cuda:0")
            for _ in range(2)]
        students[0].copy_encoder(teacher)
        students[1].load_state_dict(students[0].state_dict())
        for student in students:
            student.eval()
            student.encoder.requires_grad_(not frozen)
        encoder_shas = [semantic_digest(student.encoder.state_dict()) for student in students]
        optimizers = [torch.optim.AdamW(student.parameters(), lr=1e-4, weight_decay=1e-4)
            for student in students]
        rng = capture_rng()
        for trial, row in enumerate(selected):
            frame, legalities = dataset.batch([row], split="validation")
            records = {}
            order = (0, 1) if trial % 2 == 0 else (1, 0)
            for index in order:
                module, name = (reference, "sequential") if index == 0 else (optimized, "batched")
                student = students[index]
                optimizer = optimizers[index]
                optimizer.zero_grad(set_to_none=True)

                def operation():
                    result = module.migration_batch_loss(teacher, student, frame, legalities,
                        weights=module.MigrationLossWeights(**weights))
                    result["loss"].backward()
                    return result

                result = timed(name, operation)
                timing[-1].update(frozen_encoder=frozen, row_id=row, trial=trial)
                records[name] = numeric_metrics(result)
                print(json.dumps(dict(event="measurement", **timing[-1], metrics=records[name])), flush=True)
            for key in records["sequential"]:
                torch.testing.assert_close(torch.tensor(records["sequential"][key]),
                    torch.tensor(records["batched"][key]), atol=2e-6, rtol=2e-5)
            maximum_gradient_delta = 0.
            for (name, baseline), (_, batched) in zip(students[0].named_parameters(), students[1].named_parameters()):
                if (baseline.grad is None) != (batched.grad is None):
                    raise ValueError("gradient reachability differs: " + name)
                if baseline.grad is not None:
                    torch.testing.assert_close(baseline.grad, batched.grad, atol=2e-5, rtol=2e-4, msg=name)
                    maximum_gradient_delta = max(maximum_gradient_delta, float((baseline.grad - batched.grad).abs().max()))
            for student, optimizer in zip(students, optimizers):
                torch.nn.utils.clip_grad_norm_(student.parameters(), 1.)
                optimizer.step()
            for (name, baseline), (_, batched) in zip(students[0].named_parameters(), students[1].named_parameters()):
                check_parameter_update(baseline, batched, name=name,
                    output=args.output / "update-mismatch.json",
                    context=dict(frozen_encoder=frozen, row_id=row, trial=trial))
                for key in optimizers[0].state[baseline]:
                    torch.testing.assert_close(optimizers[0].state[baseline][key], optimizers[1].state[batched][key],
                        atol=2e-5, rtol=2e-4, msg=name + ":" + key)
            if frozen and encoder_shas != [semantic_digest(student.encoder.state_dict()) for student in students]:
                raise ValueError("frozen encoder changed")
            comparisons.append(dict(row_id=row, frozen_encoder=frozen, metrics=records,
                maximum_gradient_delta=maximum_gradient_delta,
                legal_cells=[int(legality.plays.any(-1).sum()) for legality in legalities]))
            if semantic_digest(capture_rng()) != semantic_digest(rng):
                raise ValueError("deterministic migration consumed global RNG")
            if teacher_sha != semantic_digest(teacher.state_dict()) or any(
                    parameter.grad is not None for parameter in teacher.parameters()):
                raise ValueError("frozen teacher changed")
            students[1].load_state_dict(students[0].state_dict())
            optimizers[1].load_state_dict(deepcopy(optimizers[0].state_dict()))
            restore_rng(rng)
        del students, optimizers
    medians = {name: statistics.median(item["seconds"] for item in timing if item["implementation"] == name)
        for name in ("sequential", "batched")}
    ratios = [timing[offset]["seconds"] / timing[offset + 1]["seconds"]
        if timing[offset]["implementation"] == "sequential" else timing[offset + 1]["seconds"] / timing[offset]["seconds"]
        for offset in range(0, len(timing), 2)]
    report = dict(status="passed_limited_scope", dataset_identity=dataset.identity,
        deployment_sha256=hashlib.sha256((args.root / "deployment.json").read_bytes()).hexdigest(),
        teacher_unchanged=True, global_rng_unchanged=True, comparisons=comparisons,
        timing=timing, medians=medians, paired_median_speedup=statistics.median(ratios),
        scope="three real validation frames, six single-row forward/backward comparisons; not full-training throughput",
        training_admissible=False, formal_g2_pass=False)
    from native_runner.training.fair_final.storage import durable_write, json_bytes
    durable_write(args.output / "report.json", json_bytes(report))
    print(json.dumps(dict(event="completed", medians=medians,
        paired_median_speedup=report["paired_median_speedup"])), flush=True)


if __name__ == "__main__":
    main()
