import argparse
from pathlib import Path

from verify_parent import verify as verify_parent, checkpoint_content
from run_selection import GATES, invocation, pinned, read, sha
from retry_selection import checked_files, validate, safe_tree_hash
from verify_active import implementation_identity
from verify_pipeline import verified_dataset
from verify_selection_execution import verify_terminal
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest

import torch


def terminal_content(path, report, *, timeout_path, original_path, request, dataset, native_schedule=True):
    result = verify_terminal(path, report, parent_path=original_path, request=request,
        dataset=dataset, native_schedule=native_schedule)
    timeout = torch.load(timeout_path, weights_only=True, map_location="cpu")["payload"]
    payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
    prefix = timeout["history"]
    previous = timeout["latest"]["payload"]["cursor"]["optimizer_steps"]
    if (result["latest_optimizer_steps"] <= previous or payload["history"][:len(prefix)] != prefix):
        raise ValueError("retry terminal lost timeout history or made no progress")
    if (payload["best_index"] < len(prefix)
            and semantic_digest(payload["best"]) != semantic_digest(timeout["best"])):
        raise ValueError("retry terminal changed inherited timeout best state")
    result["timeout_history_prefix_verified"] = True
    result["timeout_optimizer_steps"] = previous
    return result


def verify(attempt, *, launch_sha, receipt_sha, data_roots):
    attempt = Path(attempt).resolve()
    pinned(attempt / "launch.json", launch_sha)
    launch = read(attempt / "launch.json")
    pinned(attempt / "preparation.json", launch["preparation_sha256"])
    prepared = read(attempt / "preparation.json")
    pinned(attempt / "request.json", launch["request_sha256"])
    request = read(attempt / "request.json")
    validate(request)
    if (launch["schema"] != "FairFinalSelectionTimeoutLaunch/v1"
            or launch["remote"] != request["remote"]
            or prepared["request_sha256"] != launch["request_sha256"]
            or prepared["terminal_verifier_sha256"] != sha(__file__)):
        raise ValueError("retry launch or terminal verifier identity differs")
    proof = request["verification"]
    if proof != verify_parent(prepared["parent_local"], launch_sha=proof["parent_launch_sha256"],
            receipt_sha=proof["parent_receipt_sha256"], data_roots=data_roots):
        raise ValueError("retry parent verification differs")
    parent = Path(prepared["parent_local"])
    original_inventory = read(parent / "native-download-receipt.json")["remote_inventory"]
    if (request["files"] != original_inventory["files"]
            or request["parent_processes"] != [original_inventory["remote_process"]["process"],
                read(parent / "selection/status.json")["process"]]):
        raise ValueError("retry inputs or process identity differ from verified timeout archive")
    pinned(attempt / "receipt.json", receipt_sha)
    receipt = read(attempt / "receipt.json")
    inventory = receipt["inventory"]
    if (receipt["schema"] != "FairFinalSelectionTimeoutReceipt/v1"
            or receipt["launch_sha256"] != launch_sha or inventory["running"]
            or inventory["process"] != launch["process"] or inventory["lease_available"] is not True
            or receipt["status"] != "terminal_archive_ready_for_independent_verification"
            or inventory["state"] != receipt["status"] or inventory["request_sha256"] != launch["request_sha256"]
            or any(record[name] is not value for record in (receipt, inventory, launch)
                for name, value in GATES.items())):
        raise ValueError("retry archive is not a stopped terminal result")
    archive = attempt / "archive"
    checked_files(archive, inventory["files"])
    if safe_tree_hash(archive) != inventory["files"]:
        raise ValueError("retry archive contains unreceipted files")
    pinned(archive / "request.json", launch["request_sha256"])
    if read(archive / "worker-process.json") != launch["process"]:
        raise ValueError("retry worker identity differs")
    for name, expected in request["runtime_sha256"].items():
        pinned(archive / name, expected)
    output = archive / "selection"
    checked_files(output, {name: metadata for name, metadata in request["files"].items()
        if Path(name).parts[0] in ("inputs", "deployment")})
    pinned(output / "resume-input.pt", proof["checkpoint_sha256"])
    if (archive / "failure.json").exists() or (output / "failure.json").exists():
        raise ValueError("failed retry remains quarantined")
    execution, completion = read(output / "execution.json"), read(output / "completion.json")
    config = read(archive / "parent-remote-config.json")
    command, environment = invocation(Path(request["remote"]) / "selection", source=config["source"],
        data_root=config["data_root"], device="cuda:0", python=config["python"])
    command += ["--resume-sha", proof["checkpoint_sha256"]]
    environment["CUDA_VISIBLE_DEVICES"] = config["gpu_uuid"]
    if (execution["schema"] != "FairFinalSelectionTimeoutExecution/v1"
            or execution["command"] != command or execution["environment"] != environment
            or execution["runtime_sha256"] != request["runtime_sha256"]
            or execution["timeout_seconds"] != request["timeout_seconds"]
            or completion["status"] != "completed_pending_independent_content_verification"
            or completion["exit_code"] != 0
            or completion["execution_sha256"] != sha(output / "execution.json")
            or any(record["request_sha256"] != launch["request_sha256"] for record in (execution, completion))
            or not 0 <= completion["seconds"] <= request["timeout_seconds"] + 60
            or any(record[name] is not value for record in (execution, completion)
                for name, value in GATES.items())
            or set(completion["artifacts"]) != {"selected.pt", "selected.report.json"}):
        raise ValueError("retry frozen execution or completion differs")
    if read(output / "status.json")["process"] != completion["process"]:
        raise ValueError("retry training process differs")
    checked_files(output, completion["artifacts"])
    original = read(output / "inputs/selection-handoff.json")
    dataset, audit, source = verified_dataset(read(output / "deployment/private-plan.json"), data_roots)
    if (dataset.identity != proof["dataset_identity"] or audit["series_sha"] != original["series_sha"]
            or implementation_identity(output / "deployment") != proof["implementation_sha"]):
        raise ValueError("retry rebuilt dataset or implementation differs")
    checkpoint_content(output / "resume-input.pt", expected_sha=proof["checkpoint_sha256"],
        original_path=output / "inputs/parent-checkpoint.pt", request=original,
        dataset=dataset, teacher_sha=source["source_network_sha"])
    content = terminal_content(output / "selected.pt", read(output / "selected.report.json"),
        timeout_path=output / "resume-input.pt", original_path=output / "inputs/parent-checkpoint.pt",
        request=original, dataset=dataset)
    return dict(status="passed_independent_retry_terminal_content_scope", content=content,
        launch_sha256=launch_sha, receipt_sha256=receipt_sha,
        native_game_provenance_reverified=False, verifier_sha256=sha(__file__), **GATES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--data-roots", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = vars(parser.parse_args())
    output = args.pop("output")
    report = verify(**args)
    durable_write(output, json_bytes(report))
    print(json_bytes(report).decode(), flush=True)
