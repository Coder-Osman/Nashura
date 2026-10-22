import argparse
import json
import math
from pathlib import Path
import random

from verify_active import checked_checkpoint, implementation_identity
from verify_execution import pinned, read
from verify_native_resume import checkpoint_state, sha
from verify_pipeline import verified_dataset, verify_selection
from run_selection import GATES, SCHEMA, checked_handoff, invocation, payload_files
from native_runner.training.fair_final.checkpoint import cpu_state, validate_rng
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest

import torch


def checked_cursor(trainer, dataset, parent_cursor):
    cursor = trainer["cursor"]
    fixed = ("seed", "batch_size", "micro_batch_size", "frozen_epochs", "trainable_epochs")
    if (any(cursor[name] != parent_cursor[name] for name in fixed)
            or trainer["identity"] != dataset.identity or trainer["splits"] != dataset.splits):
        raise ValueError("terminal selection cursor configuration or dataset differs")
    for name in (*fixed, "completed_epochs", "offset", "optimizer_steps", "consumed"):
        if type(cursor[name]) is not int or cursor[name] < 0:
            raise ValueError("terminal selection integer cursor differs")
    size, batch = len(dataset.splits["train"]), cursor["batch_size"]
    epochs = cursor["completed_epochs"]
    if (not batch or not 1 <= cursor["micro_batch_size"] <= batch
            or cursor["frozen_epochs"] != 2 or cursor["trainable_epochs"] != 10
            or not 0 <= epochs <= 12 or cursor["offset"] != 0
            or cursor["optimizer_steps"] != epochs * math.ceil(size / batch)
            or cursor["consumed"] != epochs * size):
        raise ValueError("terminal selection completed-epoch accounting differs")
    sampler = random.Random(cursor["seed"])
    order = list(range(size))
    for _ in range(min(epochs + 1, 12)):
        sampler.shuffle(order)
    if cursor["order"] != order or cursor["rng"] != sampler.getstate():
        raise ValueError("terminal selection sampler order or RNG differs")
    validate_rng(trainer["rng"], topology=False)
    cpu_state(trainer)
    return cursor


def verify_terminal(path, report, *, parent_path, request, dataset, native_schedule=True):
    parent_state, identity, splits = checkpoint_state(parent_path, request["parent_checkpoint_sha256"])
    if (parent_state != request["parent_checkpoint_state"] or identity != dataset.identity
            or splits != dataset.splits or parent_state["steps"] != 7):
        raise ValueError("selection continuation parent differs")
    parent = torch.load(parent_path, weights_only=True, map_location="cpu")["payload"]
    state, payload = verify_selection(path, report, dataset=dataset,
        implementation_sha=request["implementation_sha"], config=request["config"],
        weights=request["loss_weights"], series_sha=request["series_sha"])
    cursor = parent["latest"]["payload"]["cursor"]
    if native_schedule and (len(splits["train"]) != 352 or cursor["batch_size"] != 256
            or cursor["micro_batch_size"] != 1 or cursor["seed"] != 20261022):
        raise ValueError("native terminal selection batch schedule differs")
    prefix = parent["history"]
    if (parent["terminal"] is not None or payload["terminal"] not in ("EARLY_STOP", "MAX_EPOCHS")
            or state["steps"] <= parent_state["steps"] or state["teacher"] != parent_state["teacher"]
            or payload["history"][:len(prefix)] != prefix):
        raise ValueError("selection did not reach a terminal continuation of its parent")
    for name in ("latest", "best"):
        trainer = payload[name]["payload"]
        saved_cursor = checked_cursor(trainer, dataset, cursor)
        if (trainer["weights"] != request["loss_weights"]
                or trainer["teacher_network_sha"] != parent_state["teacher"]):
            raise ValueError("terminal selection teacher or loss differs")
        if name == "best":
            selected = payload["history"][payload["best_index"]]
            if (saved_cursor["completed_epochs"] != selected["epoch"]
                    or saved_cursor["optimizer_steps"] != selected["optimizer_steps"]):
                raise ValueError("selected trainer cursor differs from validation history")
    if (payload["best_index"] < len(prefix)
            and semantic_digest(payload["best"]) != semantic_digest(parent["best"])):
        raise ValueError("unchanged best selection lost its original trainer state")
    return dict(terminal=payload["terminal"], latest_optimizer_steps=state["steps"],
        selected_epoch=payload["history"][payload["best_index"]]["epoch"],
        selected_network_sha=report["network_sha"], native_schedule_verified=native_schedule,
        parent_history_prefix_verified=True, state=state)


def verify(directory, *, execution_sha, completion_sha, data_roots):
    directory = Path(directory)
    pinned(directory / "execution.json", execution_sha)
    pinned(directory / "completion.json", completion_sha)
    execution, completion = read(directory / "execution.json"), read(directory / "completion.json")
    if ((directory / "failure.json").exists() or execution["schema"] != SCHEMA
            or completion["status"] != "completed_pending_independent_content_verification"
            or completion["exit_code"] != 0 or completion["execution_sha256"] != execution_sha
            or any(record[name] is not value for record in (execution, completion)
                for name, value in GATES.items()) or len(data_roots) != 1):
        raise ValueError("failed or incomplete selection execution remains quarantined")
    request = checked_handoff(directory / "inputs", execution["handoff_file_sha256"])
    files, manifest = payload_files(directory / "inputs", request)
    for name, data in files.items():
        if (directory / "deployment" / name).read_bytes() != data:
            raise ValueError("selection deployed bytes differ from frozen payload")
    command, environment = invocation(execution["root"], source=execution["source"],
        data_root=execution["data_root"], device=execution["device"], python=execution["python"])
    command += ["--resume-sha", request["parent_checkpoint_sha256"]]
    if (execution["source_sha256"] != manifest or execution["command"] != command
            or execution["environment"] != environment or execution["worker_sha256"] != sha(Path(__file__).with_name("run_selection.py"))
            or execution["handoff_sha"] != request["handoff_sha"]
            or execution["parent_checkpoint_sha256"] != request["parent_checkpoint_sha256"]
            or type(execution["timeout_seconds"]) is not int or not 1800 <= execution["timeout_seconds"] <= 28800
            or execution["device"] not in ("cpu", "cuda:0")
            or type(completion["process"]["pid"]) is not int or completion["process"]["pid"] <= 0
            or not isinstance(completion["process"]["start_ticks"], str)
            or not completion["process"]["start_ticks"].isdigit()
            or not isinstance(completion["seconds"], (int, float))
            or not 0 <= completion["seconds"] <= execution["timeout_seconds"] + 60):
        raise ValueError("selection execution identity or bounded command differs")
    if set(completion["artifacts"]) != {"selected.pt", "selected.report.json"}:
        raise ValueError("selection completion artifacts differ")
    for name, metadata in completion["artifacts"].items():
        pinned(directory / name, metadata["sha256"])
        if (directory / name).stat().st_size != metadata["bytes"]:
            raise ValueError("selection completion byte count differs")
    deployment = directory / "deployment"
    dataset, audit, source = verified_dataset(read(deployment / "private-plan.json"), data_roots)
    implementation = implementation_identity(deployment)
    if (implementation != request["implementation_sha"] or dataset.identity != request["dataset_identity"]
            or {name: len(rows) for name, rows in dataset.splits.items()} != request["split_rows"]
            or audit["series_sha"] != request["series_sha"]):
        raise ValueError("selection rebuilt data or implementation identity differs")
    parent_path = directory / "inputs/parent-checkpoint.pt"
    checked_checkpoint(parent_path, request["parent_checkpoint_sha256"], dataset=dataset,
        implementation_sha=implementation, config=request["config"], weights=request["loss_weights"],
        teacher_sha=source["source_network_sha"])
    content = verify_terminal(directory / "selected.pt", read(directory / "selected.report.json"),
        parent_path=parent_path, request=request, dataset=dataset)
    return dict(status="passed_independent_terminal_selection_content_scope", content=content,
        execution_sha256=execution_sha, completion_sha256=completion_sha,
        dataset_identity=dataset.identity, implementation_sha=implementation,
        frozen_deployment_verified=True, native_game_provenance_reverified=False,
        verifier_sha256=sha(__file__), **GATES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--execution-sha", required=True)
    parser.add_argument("--completion-sha", required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.directory, execution_sha=args.execution_sha,
        completion_sha=args.completion_sha, data_roots=args.data_roots)
    durable_write(args.output, json_bytes(report))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
