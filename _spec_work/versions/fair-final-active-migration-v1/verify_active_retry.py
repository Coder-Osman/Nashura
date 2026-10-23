import argparse
from pathlib import Path

from retry_active import failed_parent, request_for
from run_selection import GATES, publish, read, sha
from verify_active import implementation_identity, verify_comparisons
from verify_execution import archived, pinned
from verify_pipeline import verified_dataset, verify_benchmark
from round_selection import SelectionConfig


def execution_link(evidence, receipt, launch, request, parent_phases):
    evidence = Path(evidence)
    pinned(evidence / "retry-input.json", launch["retry_input_sha256"])
    if (read(evidence / "retry-input.json") != request
            or "supervisor-failure.json" in receipt["files"]
            or any(launch[name] is not value for name, value in GATES.items())):
        raise ValueError("retry request, execution failure or scope differs")
    for name, metadata in request["files"].items():
        if receipt["files"].get(name) != metadata:
            raise ValueError("retry did not preserve parent input bytes")
    phases = read(evidence / "phase-results.json")
    completion = read(evidence / "completion.json")
    if (len(phases) != 1 or phases[0]["phase"] != "trainable" or phases[0]["exit_code"] != 0
            or completion["exit_code"] != 0
            or completion["status"] != "completed_retry_pending_independent_verification"
            or completion["retry_input_sha256"] != launch["retry_input_sha256"]
            or any(completion[name] is not value for name, value in GATES.items())):
        raise ValueError("retry requires one successful fresh trainable process")
    process = phases[0]["process"]
    prior = [item["process"] for item in parent_phases]
    if (type(process["pid"]) is not int or process["pid"] <= 0
            or not isinstance(process["start_ticks"], str) or not process["start_ticks"].isdigit()
            or process in prior or process == launch["process"] or completion["process"] != process):
        raise ValueError("retry process identity differs or was reused")
    expected_command = ["/home/zhichun/firstlight_CR/.venv/bin/python", "-B", "-u",
        launch["remote"] + "/probe_active_resume.py", "--root", launch["remote"],
        "--data-root", request["data_root"], "--source", request["source"],
        "--mode", "trainable", "--reference-sha",
        request["files"]["active-comparison/reference.json"]["sha256"]]
    expected_environment = dict(CUDA_VISIBLE_DEVICES=str(request["device_index"]),
        PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", CUBLAS_WORKSPACE_CONFIG=":4096:8")
    if completion["command"] != expected_command or completion["environment"] != expected_environment:
        raise ValueError("retry command or deterministic environment differs")
    if read(evidence / "active-comparison/trainable.json")["process"] != process:
        raise ValueError("retry report process differs")
    for name in ("reference", "frozen"):
        if read(evidence / ("active-comparison/" + name + ".json"))["process"] != next(
                item["process"] for item in parent_phases if item["phase"] == name):
            raise ValueError("preserved parent report process differs")
    return process


def verify_retry(parent, attempt, *, parent_launch_sha, parent_receipt_sha,
        launch_sha, receipt_sha, data_roots):
    parent_launch, parent_evidence, _ = archived(parent,
        launch_sha=parent_launch_sha, receipt_sha=parent_receipt_sha)
    parent_phases = failed_parent(parent_evidence)
    launch, evidence, receipt = archived(attempt, launch_sha=launch_sha, receipt_sha=receipt_sha)
    if (Path(parent).resolve() == Path(attempt).resolve() or launch["remote"] == parent_launch["remote"]
            or launch["parent_launch_sha256"] != parent_launch_sha
            or launch["parent_receipt_sha256"] != parent_receipt_sha or len(data_roots) != 1
            or launch["source_sha256"] != parent_launch["source_sha256"]
            or launch["payload_sha256"] != parent_launch["payload_sha256"]):
        raise ValueError("retry parent, frozen implementation or data linkage differs")
    pinned(evidence / "payload.tar.gz", parent_launch["payload_sha256"])
    supplied = read(evidence / "retry-input.json")
    request = request_for(parent, launch_sha=parent_launch_sha, receipt_sha=parent_receipt_sha,
        device_index=supplied["device_index"], timeout_seconds=supplied["timeout_seconds"])
    process = execution_link(evidence, receipt, launch, request, parent_phases)
    dataset, audit, source = verified_dataset(read(evidence / "private-plan.json"), data_roots)
    deployment_sha = sha(evidence / "deployment.json")
    benchmark = verify_benchmark(read(evidence / "benchmark/report.json"),
        deployment_sha=deployment_sha, identity=dataset.identity,
        validation_rows=dataset.splits["validation"])
    config = read(evidence / "selection-config.json")
    SelectionConfig(**config)
    directory = evidence / "active-comparison"
    content = verify_comparisons(directory,
        report_shas={name: sha(directory / (name + ".json")) for name in ("reference", "frozen", "trainable")},
        dataset=dataset, implementation_sha=implementation_identity(evidence), deployment_sha=deployment_sha,
        config=config, weights=read(evidence / "loss-weights.json"), teacher_sha=source["source_network_sha"])
    return dict(status="passed_active_retry_chain_and_two_stage_checkpoint_scope", content=content,
        parent_status="failed_trainable_timeout_preserved", parent_failure_sha256=request["parent_failure_sha256"],
        parent_launch_sha256=parent_launch_sha, parent_receipt_sha256=parent_receipt_sha,
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, process=process, benchmark=benchmark,
        dataset_identity=dataset.identity, split_rows={name: len(rows) for name, rows in dataset.splits.items()},
        series_sha=audit["series_sha"], frozen_deployment_verified=True,
        native_game_provenance_reverified=False, verifier_sha256=sha(__file__), **GATES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("attempt", type=Path)
    for name in ("parent-launch-sha", "parent-receipt-sha", "launch-sha", "receipt-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_retry(args.parent, args.attempt, parent_launch_sha=args.parent_launch_sha,
        parent_receipt_sha=args.parent_receipt_sha, launch_sha=args.launch_sha,
        receipt_sha=args.receipt_sha, data_roots=args.data_roots)
    publish(args.output, report)
    print(report["status"], flush=True)
