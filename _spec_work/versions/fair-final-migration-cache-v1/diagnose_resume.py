import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text())
    root = Path(settings["parent"])
    for name, expected in settings["deployment"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("frozen diagnostic dependency differs: " + name)
    sys.path[:0] = [str(root / "_spec_work/versions/fair-final-migration-selection-v1"),
        str(root / "_spec_work/versions/fair-final-migration-series-v1"), str(root / "firstlight_CR")]
    import torch
    if settings.get("deterministic", False):
        torch.use_deterministic_algorithms(True)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
    import round_selection
    from series_training import SeriesMigrationDataset
    from native_runner.training.fair_final.model import ConditionalPolicyValue
    from native_runner.training.fair_migration import MigrationLossWeights
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.model import load_checkpoint

    torch.set_num_threads(1)
    started = time.monotonic()
    checkpoint = root / "round0.pt"
    if hashlib.sha256(checkpoint.read_bytes()).hexdigest() != settings["checkpoint_sha"]:
        raise ValueError("diagnostic checkpoint differs")
    payload = torch.load(checkpoint, weights_only=True, map_location="cpu")["payload"]
    plan = json.loads((root / "private-plan.json").read_text())
    teacher, _, _ = load_checkpoint(settings["source"], device="cuda:0",
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    dataset = SeriesMigrationDataset(plan, [settings["data_root"]], teacher=teacher)
    weights = MigrationLossWeights(**json.loads((root / "loss-weights.json").read_text()))
    best = round_selection._restore(payload["best"], dataset, weights, bundle=None, device="cuda:0")
    torch.manual_seed(payload["best"]["payload"]["cursor"]["seed"])
    original = ConditionalPolicyValue(encoder_config=teacher.policy.backbone.config).to("cuda:0")
    original.copy_encoder(teacher)
    report = dict(training_admissible=False, formal_g2_pass=False,
        checkpoint_sha=settings["checkpoint_sha"], stored=payload["history"][payload["best_index"]]["validation"],
        original_network_sha=semantic_digest(original.state_dict()),
        restored_network_sha=semantic_digest(best.student.state_dict()), samples=[],
        backend=dict(torch=torch.__version__, cuda=torch.version.cuda,
            deterministic=torch.are_deterministic_algorithms_enabled(),
            cudnn_deterministic=torch.backends.cudnn.deterministic,
            cudnn_benchmark=torch.backends.cudnn.benchmark,
            matmul_precision=torch.get_float32_matmul_precision()))
    original.eval()
    best.student.eval()
    rows = dataset.splits["validation"]
    for index in (0, len(rows) // 2, len(rows) - 1):
        frame, legalities = dataset.batch([rows[index]], split="validation")
        metrics = []
        with torch.no_grad():
            for model in (original, best.student, best.student):
                result = round_selection.migration_batch_loss(teacher, model, frame, legalities, weights=weights)
                metrics.append({key: float(result[key]) for key in round_selection.METRICS})
        report["samples"].append(dict(row_id=rows[index], frame_sha=semantic_digest(frame), metrics=metrics))
        print(json.dumps(dict(event="sample", sample=report["samples"][-1])), flush=True)
    if not settings.get("samples_only", False):
        report["measured"] = round_selection.evaluate_validation(best, dataset)
        report["delta"] = {key: report["measured"][key] - report["stored"][key]
            for key in round_selection.METRICS}
        report["exact_validation_match"] = semantic_digest(report["measured"]) == semantic_digest(report["stored"])
    report["sample_repeats_exact"] = all(sample["metrics"][1] == sample["metrics"][2]
        for sample in report["samples"])
    report["reconstructed_vs_restored_exact"] = all(sample["metrics"][0] == sample["metrics"][1]
        for sample in report["samples"])
    report["seconds"] = time.monotonic() - started
    output = args.settings.parent / "report.json"
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(event="completed", delta=report.get("delta"), seconds=report["seconds"],
        sample_repeats_exact=report["sample_repeats_exact"],
        reconstructed_vs_restored_exact=report["reconstructed_vs_restored_exact"])), flush=True)


if __name__ == "__main__":
    main()
