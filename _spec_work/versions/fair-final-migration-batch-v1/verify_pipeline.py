import argparse
import json
import math
from pathlib import Path
import statistics
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / name) for name in (
    "fair-final-migration-cache-v1", "fair-final-migration-integration-v1",
    "fair-final-migration-selection-v1", "fair-final-migration-series-v1")]
sys.path.insert(0, str(ROOT.parents[2] / "firstlight_CR"))

import torch

from migration_series import audit_completed_series, ReadOnlyMigrationStore
from round_selection import _validate_history, SelectionConfig, validation_identity
from verify_execution import archived, read, pinned, SERIES, SELECTION, PROBE
from verify_native_resume import checkpoint_state, sha, verify_comparison
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


def implementation_identity(evidence):
    paths = [SERIES + name for name in ("series_training.py", "verified_store.py",
        "deterministic_runtime.py", "migration_series.py")]
    paths += ["firstlight_CR/native_runner/training/" + name for name in (
        "fair_migration_trainer.py", "fair_final/batched_migration.py", "fair_migration.py")]
    series_sha = semantic_digest({Path(name).name: sha(evidence / name) for name in paths})
    return semantic_digest(dict(selection=sha(evidence / SELECTION / "round_selection.py"),
        entrypoint=sha(evidence / SELECTION / "train_round.py"), series=series_sha))


def verify_benchmark(report, *, deployment_sha, identity, validation_rows):
    if (report["status"] != "passed_limited_scope" or report["training_admissible"] is not False
            or report["formal_g2_pass"] is not False or report["teacher_unchanged"] is not True
            or report["global_rng_unchanged"] is not True or report["dataset_identity"] != identity
            or report["deployment_sha256"] != require_sha(deployment_sha)):
        raise ValueError("benchmark identity or engineering scope differs")
    selected = [validation_rows[0], validation_rows[len(validation_rows) // 2], validation_rows[-1]]
    comparisons, timing = report["comparisons"], report["timing"]
    if len(comparisons) != 6 or len(timing) != 12:
        raise ValueError("six paired measurements required")
    ratios = []
    for index, comparison in enumerate(comparisons):
        frozen, trial = index < 3, index % 3
        row = selected[trial]
        if (type(comparison["frozen_encoder"]) is not bool or comparison["frozen_encoder"] != frozen
                or comparison["row_id"] != row or not comparison["legal_cells"]
                or any(type(value) is not int or value < 0 for value in comparison["legal_cells"])
                or not math.isfinite(comparison["maximum_gradient_delta"])
                or comparison["maximum_gradient_delta"] < 0):
            raise ValueError("benchmark comparison frame differs")
        pair = timing[2 * index:2 * index + 2]
        names = ["sequential", "batched"] if trial % 2 == 0 else ["batched", "sequential"]
        if [item["implementation"] for item in pair] != names:
            raise ValueError("benchmark alternating pair differs")
        for item in pair:
            if (item["row_id"] != row or item["trial"] != trial
                    or type(item["frozen_encoder"]) is not bool or item["frozen_encoder"] != frozen
                    or type(item["seconds"]) not in (int, float)
                    or not math.isfinite(item["seconds"]) or item["seconds"] <= 0):
                raise ValueError("benchmark timing frame or duration differs")
        metrics = comparison["metrics"]
        if set(metrics) != {"sequential", "batched"} or set(metrics["sequential"]) != set(metrics["batched"]):
            raise ValueError("benchmark metric support differs")
        for key, baseline in metrics["sequential"].items():
            batched = metrics["batched"][key]
            if (not math.isfinite(baseline) or not math.isfinite(batched)
                    or abs(baseline - batched) > 2e-6 + 2e-5 * abs(batched)):
                raise ValueError("benchmark loss comparison differs")
        durations = {item["implementation"]: item["seconds"] for item in pair}
        ratios.append(durations["sequential"] / durations["batched"])
    medians = {name: statistics.median(item["seconds"] for item in timing if item["implementation"] == name)
        for name in ("sequential", "batched")}
    speedup = statistics.median(ratios)
    if report["medians"] != medians or report["paired_median_speedup"] != speedup:
        raise ValueError("benchmark summary differs from paired timings")
    return dict(medians=medians, paired_median_speedup=speedup,
        scope="archived six-pair accounting; CUDA tensor comparisons belong to the frozen probe")


def verify_selection(path, report, *, dataset, implementation_sha, config, weights, series_sha):
    state, identity, splits = checkpoint_state(path, report["checkpoint_sha"])
    payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
    trainer = payload["latest"]["payload"]
    if (identity != dataset.identity or splits != dataset.splits
            or payload["implementation_sha"] != implementation_sha or payload["config"] != config
            or payload["validation_sha"] != validation_identity(dataset) or trainer["weights"] != weights):
        raise ValueError("selection implementation, data or configuration differs")
    cursor = trainer["cursor"]
    history_trainer = SimpleNamespace(completed_epochs=cursor["completed_epochs"],
        splits=splits, batch_size=cursor["batch_size"], offset=cursor["offset"],
        lineage=trainer.get("lineage", {}),
        weights=SimpleNamespace(**weights), student=SimpleNamespace(state_dict=lambda: trainer["network"]))
    run = SimpleNamespace(trainer=history_trainer, history=payload["history"], terminal=payload["terminal"])
    _validate_history(run, dataset)
    expected = dict(schema="FairFinalMigrationSelectionReport/v1", checkpoint_sha=sha(path),
        implementation_sha=implementation_sha, config=config, weights=weights, dataset_identity=identity,
        series_sha=series_sha, validation_sha=payload["validation_sha"], terminal=payload["terminal"],
        latest_optimizer_steps=state["steps"], selected=payload["history"][payload["best_index"]],
        history=payload["history"], network_sha=semantic_digest(payload["best"]["payload"]["network"]),
        training_admissible=False, formal_g2_pass=False)
    if report != expected:
        raise ValueError("selection report differs from checkpoint content")
    return state, payload


def verified_dataset(plan, data_roots):
    audit = audit_completed_series(plan, data_roots)
    splits = {name: [] for name in ("train", "validation", "test")}
    receipts, index = {}, {}
    stores = []
    for number, (entry, root) in enumerate(zip(plan["rounds"], data_roots)):
        store = ReadOnlyMigrationStore(root, manifest=entry["manifest"])
        stores.append(store)
        for match, task in sorted(store.manifest["matches"].items()):
            episode, record = store.load(match)
            receipts[match] = semantic_digest(record)
            for offset, row in enumerate(episode["rows"]):
                row_id = row["row_id"]
                if row_id in index:
                    raise ValueError("duplicate migration row")
                splits[task["split"]].append(row_id)
                index[row_id] = (number, match, offset, task["split"])
    source = plan["rounds"][0]["manifest"]["identity"]
    dataset_sha = (semantic_digest(dict(manifest_sha=stores[0].dataset_sha, receipts=receipts))
        if len(stores) == 1 else semantic_digest(dict(series_sha=audit["series_sha"], receipts=receipts)))
    identity = dict(source_a_sha=source["source_a_sha"], migration_adapter_sha=source["migration_adapter_sha"],
        dataset_sha=dataset_sha)
    if any(not rows for rows in splits.values()):
        raise ValueError("complete historical train, validation and test games required")
    return SimpleNamespace(identity=identity, splits=splits, index=index), audit, source


def verify_pipeline(attempt, *, launch_sha, receipt_sha, data_roots):
    launch, evidence, receipt = archived(attempt, launch_sha=launch_sha, receipt_sha=receipt_sha)
    pinned(evidence / "payload.tar.gz", launch["payload_sha256"])
    if "supervisor-failure.json" in receipt["files"]:
        raise ValueError("failed pipeline is quarantined")
    phases = read(evidence / "phase-results.json")
    completion = read(evidence / "completion.json")
    names = ["benchmark", "training", "reference", "resumed", "selection"]
    if ([phase["phase"] for phase in phases] != names or any(phase["exit_code"] != 0 for phase in phases)
            or len({(phase["process"]["pid"], phase["process"]["start_ticks"]) for phase in phases}) != 5
            or completion["exit_code"] != 0 or completion["training_admissible"] is not False
            or completion["formal_g2_pass"] is not False):
        raise ValueError("pipeline did not complete five distinct successful phases")
    dataset, audit, source = verified_dataset(read(evidence / "private-plan.json"), data_roots)
    if len(data_roots) != 1:
        raise ValueError("initial-round engineering pipeline requires one whole-game round")
    implementation = implementation_identity(evidence)
    config, weights = read(evidence / "selection-config.json"), read(evidence / "loss-weights.json")
    SelectionConfig(**config)
    benchmark = verify_benchmark(read(evidence / "benchmark/report.json"),
        deployment_sha=sha(evidence / "deployment.json"), identity=dataset.identity,
        validation_rows=dataset.splits["validation"])
    states = {}
    for name in ("round0.pt", "comparison/midpoint.pt", "comparison/reference.pt", "comparison/resumed.pt", "selected.pt"):
        path = evidence / name
        state, identity, splits = checkpoint_state(path, sha(path))
        payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
        if (identity != dataset.identity or splits != dataset.splits
                or state["teacher"] != source["source_network_sha"] or payload["implementation_sha"] != implementation
                or payload["config"] != config or payload["latest"]["payload"]["weights"] != weights
                or payload["validation_sha"] != validation_identity(dataset)):
            raise ValueError("pipeline checkpoint identity differs")
        states[name] = state
    parent, _ = verify_selection(evidence / "round0.pt", read(evidence / "round0.report.json"),
        dataset=dataset, implementation_sha=implementation, config=config, weights=weights, series_sha=audit["series_sha"])
    if parent["steps"] != 1 or read(evidence / "round0.report.json")["terminal"] is not None:
        raise ValueError("round0 must contain one nonterminal update")
    comparison = evidence / "comparison"
    reference, resumed = read(comparison / "reference.json"), read(comparison / "resumed.json")
    if (reference["before"] != parent or reference["input_checkpoint_sha256"] != sha(evidence / "round0.pt")
            or resumed["input_checkpoint_sha256"] != sha(comparison / "midpoint.pt")
            or reference["dataset_identity"] != dataset.identity or resumed["dataset_identity"] != dataset.identity
            or reference["probe_sha256"] != sha(evidence / PROBE) or resumed["probe_sha256"] != sha(evidence / PROBE)):
        raise ValueError("pipeline comparison inputs differ")
    content = verify_comparison(comparison, reference_sha=sha(comparison / "reference.json"),
        resumed_sha=sha(comparison / "resumed.json"))
    selected, selected_payload = verify_selection(evidence / "selected.pt", read(evidence / "selected.report.json"),
        dataset=dataset, implementation_sha=implementation, config=config, weights=weights, series_sha=audit["series_sha"])
    reference_payload = torch.load(comparison / "reference.pt", weights_only=True, map_location="cpu")["payload"]
    prefix = reference_payload["history"]
    if (selected_payload["terminal"] is None or selected["steps"] < states["comparison/reference.pt"]["steps"]
            or selected_payload["history"][:len(prefix)] != prefix):
        raise ValueError("selection did not finish the reference continuation")
    return dict(status="passed_pipeline_archive_and_checkpoint_scope", benchmark=benchmark, content=content,
        dataset_identity=dataset.identity, split_rows={name: len(rows) for name, rows in dataset.splits.items()},
        implementation_sha=implementation, terminal=selected_payload["terminal"],
        selected_epoch=selected_payload["history"][selected_payload["best_index"]]["epoch"],
        latest_optimizer_steps=selected["steps"], launch_sha256=launch_sha, receipt_sha256=receipt_sha,
        frozen_deployment_verified=True, native_game_provenance_reverified=False,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_pipeline(args.attempt, launch_sha=args.launch_sha, receipt_sha=args.receipt_sha,
        data_roots=args.data_roots)
    report["verifier_sha256"] = sha(__file__)
    durable_write(args.output, json_bytes(report))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
