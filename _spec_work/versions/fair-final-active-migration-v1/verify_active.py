import argparse
import json
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / name) for name in (
    "fair-final-migration-batch-v1", "fair-final-migration-cache-v1",
    "fair-final-migration-integration-v1", "fair-final-migration-selection-v1",
    "fair-final-migration-series-v1")]
sys.path.insert(0, str(ROOT.parents[2] / "firstlight_CR"))

import torch

from round_selection import METRICS, SelectionConfig, _validate_history, validation_identity
from verify_execution import SERIES, SELECTION, archived, pinned, read
from verify_native_resume import checkpoint_state, sha
from verify_pipeline import verified_dataset, verify_benchmark
from native_runner.training.fair_final.checkpoint import cpu_state, validate_rng
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_migration_trainer import validate_inputs
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


ACTIVE = tuple("firstlight_CR/native_runner/training/fair_final/" + name + ".py"
    for name in ("adapter", "learning", "model", "policy", "search")) + (
    "firstlight_CR/native_runner/training/fair_migration.py",)


def implementation_identity(evidence):
    paths = [SERIES + name for name in ("series_training.py", "verified_store.py",
        "deterministic_runtime.py", "migration_series.py")]
    paths.append("firstlight_CR/native_runner/training/fair_migration_trainer.py")
    series = semantic_digest(dict(pipeline={Path(name).name: sha(evidence / name) for name in paths},
        active={name: sha(evidence / name) for name in ACTIVE}))
    return semantic_digest(dict(selection=sha(evidence / SELECTION / "round_selection.py"),
        entrypoint=sha(evidence / SELECTION / "train_round.py"), series=series))


def checked_checkpoint(path, expected_sha, *, dataset, implementation_sha, config, weights, teacher_sha):
    state, identity, splits = checkpoint_state(path, expected_sha)
    payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
    trainer = payload["latest"]["payload"]
    validate_inputs(identity, splits)
    if (identity != dataset.identity or splits != dataset.splits
            or payload["implementation_sha"] != require_sha(implementation_sha)
            or payload["config"] != config or trainer["weights"] != weights
            or payload["validation_sha"] != validation_identity(dataset)
            or state["teacher"] != require_sha(teacher_sha) or payload["terminal"] is not None):
        raise ValueError("active checkpoint data, implementation or selection identity differs")
    cursor = trainer["cursor"]
    for name in ("seed", "batch_size", "micro_batch_size", "optimizer_steps", "completed_epochs",
            "offset", "consumed", "frozen_epochs", "trainable_epochs"):
        if type(cursor[name]) is not int or cursor[name] < 0:
            raise ValueError("active checkpoint cursor integer differs")
    size = len(splits["train"])
    batch = cursor["batch_size"]
    if not batch or not 1 <= cursor["micro_batch_size"] <= batch:
        raise ValueError("active checkpoint batch schedule differs")
    epochs, in_epoch = divmod(cursor["optimizer_steps"], math.ceil(size / batch))
    if (cursor["frozen_epochs"] != 2 or cursor["trainable_epochs"] != 10
            or epochs >= 12 or cursor["completed_epochs"] != epochs
            or cursor["offset"] != in_epoch * batch
            or cursor["consumed"] != epochs * size + cursor["offset"]):
        raise ValueError("active checkpoint epoch or sample accounting differs")
    sampler = random.Random(cursor["seed"])
    order = list(range(size))
    for _ in range(epochs + 1):
        sampler.shuffle(order)
    if cursor["order"] != order or cursor["rng"] != sampler.getstate():
        raise ValueError("active checkpoint order or sampler RNG differs")
    validate_rng(trainer["rng"], topology=False)
    cpu_state(trainer)
    history_trainer = SimpleNamespace(completed_epochs=epochs, splits=splits, batch_size=batch,
        offset=cursor["offset"], weights=SimpleNamespace(**weights),
        student=SimpleNamespace(state_dict=lambda: trainer["network"]))
    _validate_history(SimpleNamespace(trainer=history_trainer, history=payload["history"],
        terminal=payload["terminal"]), dataset)
    encoder = {name: tensor for name, tensor in trainer["network"].items() if name.startswith("encoder.")}
    if not encoder:
        raise ValueError("active checkpoint lacks encoder tensors")
    return state, semantic_digest(encoder), cursor


def verify_comparisons(directory, *, report_shas, dataset, implementation_sha, deployment_sha,
        config, weights, teacher_sha, native_schedule=True):
    directory = Path(directory)
    if set(report_shas) != {"reference", "frozen", "trainable"}:
        raise ValueError("three pinned active reports required")
    reports = {}
    for name, expected in report_shas.items():
        pinned(directory / (name + ".json"), expected)
        report = read(directory / (name + ".json"))
        if (report["mode"] != name or report["dataset_identity"] != dataset.identity
                or report["implementation_sha"] != require_sha(implementation_sha)
                or report["deployment_sha256"] != require_sha(deployment_sha)
                or report["training_admissible"] is not False or report["formal_g2_pass"] is not False):
            raise ValueError("active report identity or engineering scope differs")
        reports[name] = report
    reference = reports["reference"]
    if (set(reference["branches"]) != {"frozen", "trainable"}
            or reference["initial"]["steps"] != 0 or reference["initial"]["teacher"] != teacher_sha):
        raise ValueError("active reference initial state or branches differ")
    updates = reference["updates"]
    size = len(dataset.splits["train"])
    results, encoders = {}, {}
    for name, middle_steps, final_steps, offset in (("frozen", 1, 3, 1), ("trainable", 5, 7, 5)):
        branch, resumed = reference["branches"][name], reports[name]
        if (resumed["status"] != "passed_limited_scope" or resumed["stage"] != name
                or resumed["exact_model_optimizer_rng_sampler_selection"] is not True):
            raise ValueError("active resume scope or stage differs")
        checkpoints = {}
        for kind, expected in (("midpoint", branch["midpoint_sha256"]),
                ("reference", branch["reference_checkpoint_sha256"]),
                ("resumed", resumed["resumed_checkpoint_sha256"])):
            checkpoints[kind] = checked_checkpoint(directory / (name + "-" + kind + ".pt"), expected,
                dataset=dataset, implementation_sha=implementation_sha, config=config,
                weights=weights, teacher_sha=teacher_sha)
        middle, expected, actual = [checkpoints[kind][0] for kind in ("midpoint", "reference", "resumed")]
        cursor = checkpoints["midpoint"][2]
        batch, tail = cursor["batch_size"], size % cursor["batch_size"]
        if (not tail or math.ceil(size / batch) != 2
                or native_schedule and (size != 352 or batch != 256 or tail != 96
                    or cursor["micro_batch_size"] != 1)):
            raise ValueError("active two-update real batch schedule differs")
        if (middle != branch["midpoint_state"] or expected != branch["expected"]
                or actual != resumed["actual"] or expected != actual
                or middle["steps"] != middle_steps or actual["steps"] != final_steps
                or middle["network"] == actual["network"] or middle["optimizer"] == actual["optimizer"]
                or len(middle["next_ids"]) != tail or len(actual["next_ids"]) != tail
                or resumed["metrics"] != branch["metrics"] or branch["metrics"] != updates[offset:offset + 2]):
            raise ValueError("active report differs from checkpoint content or update metrics")
        encoders[name] = [checkpoints[kind][1] for kind in ("midpoint", "reference", "resumed")]
        if encoders[name][1] != encoders[name][2] or (encoders[name][0] == encoders[name][1]) != (name == "frozen"):
            raise ValueError("active encoder freeze or unfreeze differs")
        results[name] = dict(midpoint_steps=middle_steps, final_steps=final_steps,
            restored_updates=[tail, batch], state=actual)
    if len(updates) != 7:
        raise ValueError("active reference must contain seven updates")
    batch, tail = cursor["batch_size"], size % cursor["batch_size"]
    for index, metrics in enumerate(updates):
        stage = "FROZEN_ENCODER" if index < 4 else "TRAINABLE_ENCODER"
        if (metrics["rows"] != (batch if index % 2 == 0 else tail) or metrics["stage"] != stage
                or any(type(metrics[key]) not in (int, float) or not math.isfinite(metrics[key])
                    for key in (*METRICS, "gradient_norm")) or metrics["gradient_norm"] < 0
                or not math.isclose(metrics["loss"], sum(weights[key] * metrics[key] for key in METRICS[1:]),
                    rel_tol=1e-6, abs_tol=1e-6)):
            raise ValueError("active update rows, stage or loss accounting differs")
    if encoders["frozen"][1] == encoders["trainable"][0]:
        raise ValueError("active trainable preparation did not update encoder")
    return dict(status="passed_two_stage_checkpoint_content_scope", branches=results,
        report_sha256=report_shas, native_schedule_verified=native_schedule,
        frozen_deployment_verified=False, training_admissible=False, formal_g2_pass=False, gates_closed=False)


def verify_archive(attempt, *, launch_sha, receipt_sha, data_roots):
    launch, evidence, receipt = archived(attempt, launch_sha=launch_sha, receipt_sha=receipt_sha)
    pinned(evidence / "payload.tar.gz", launch["payload_sha256"])
    if "supervisor-failure.json" in receipt["files"]:
        raise ValueError("failed active attempt remains quarantined")
    phases, completion = read(evidence / "phase-results.json"), read(evidence / "completion.json")
    names = ["benchmark", "reference", "frozen", "trainable"]
    if ([item["phase"] for item in phases] != names or any(item["exit_code"] != 0 for item in phases)
            or len({(item["process"]["pid"], item["process"]["start_ticks"]) for item in phases}) != 4
            or completion["exit_code"] != 0 or completion["training_admissible"] is not False
            or completion["formal_g2_pass"] is not False or len(data_roots) != 1):
        raise ValueError("active archive requires four successful distinct processes and one data round")
    required = ["benchmark/report.json", "active-comparison/reference.json", "phase-results.json",
        "completion.json", "private-plan.json", "selection-config.json", "loss-weights.json",
        "payload.tar.gz", "deployment.json"]
    required += ["active-comparison/" + name + suffix for name in ("frozen", "trainable")
        for suffix in (".json", "-midpoint.pt", "-reference.pt", "-resumed.pt")]
    if any(name not in receipt["files"] for name in required):
        raise ValueError("active archive omits comparison artifacts")
    dataset, audit, source = verified_dataset(read(evidence / "private-plan.json"), data_roots)
    deployment_sha = sha(evidence / "deployment.json")
    benchmark = verify_benchmark(read(evidence / "benchmark/report.json"), deployment_sha=deployment_sha,
        identity=dataset.identity, validation_rows=dataset.splits["validation"])
    directory = evidence / "active-comparison"
    for phase in phases[1:]:
        report = read(directory / (phase["phase"] + ".json"))
        if report["process"] != phase["process"]:
            raise ValueError("active report process differs from supervisor identity")
    config = read(evidence / "selection-config.json")
    SelectionConfig(**config)
    content = verify_comparisons(directory,
        report_shas={name: sha(directory / (name + ".json")) for name in names[1:]},
        dataset=dataset, implementation_sha=implementation_identity(evidence), deployment_sha=deployment_sha,
        config=config, weights=read(evidence / "loss-weights.json"), teacher_sha=source["source_network_sha"])
    return dict(status="passed_active_archive_and_two_stage_checkpoint_scope", content=content,
        benchmark=benchmark, split_rows={name: len(rows) for name, rows in dataset.splits.items()},
        dataset_identity=dataset.identity, series_sha=audit["series_sha"], launch_sha256=launch_sha,
        receipt_sha256=receipt_sha, frozen_deployment_verified=True, native_game_provenance_reverified=False,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_archive(args.attempt, launch_sha=args.launch_sha, receipt_sha=args.receipt_sha,
        data_roots=args.data_roots)
    report["verifier_sha256"] = sha(__file__)
    durable_write(args.output, json_bytes(report))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
