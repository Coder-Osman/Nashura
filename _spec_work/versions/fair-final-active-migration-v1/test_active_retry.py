from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import retry_active
import retry_runtime
import verify_active_retry
from run_selection import GATES, publish, read, sha


@pytest.fixture
def failed(tmp_path):
    phases = [dict(phase=name, exit_code=124 if name == "trainable" else 0,
        process=dict(pid=index + 10, start_ticks=str(index + 20)))
        for index, name in enumerate(("benchmark", "reference", "frozen", "trainable"))]
    publish(tmp_path / "phase-results.json", phases)
    publish(tmp_path / "supervisor-failure.json",
        dict(error="RuntimeError('trainable failed: 124')", training_admissible=False))
    return tmp_path, phases


def test_timeout_parent_remains_a_failure(failed):
    directory, phases = failed
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert retry_active.failed_parent(directory) == phases
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


@pytest.mark.parametrize("change", ("completed", "wrong_exit", "partial_report", "partial_checkpoint", "reused", "scope"))
def test_other_parent_states_are_rejected(failed, change):
    directory, phases = failed
    if change == "completed":
        publish(directory / "completion.json", dict(exit_code=0))
    elif change == "wrong_exit":
        phases[-1]["exit_code"] = 1
    elif change == "reused":
        phases[-1]["process"] = phases[0]["process"]
    elif change == "scope":
        failure = read(directory / "supervisor-failure.json")
        failure["training_admissible"] = True
        (directory / "supervisor-failure.json").write_text(json.dumps(failure))
    else:
        path = directory / "active-comparison" / (
            "trainable.json" if change == "partial_report" else "trainable-resumed.pt")
        path.parent.mkdir()
        path.write_bytes(b"partial")
    (directory / "phase-results.json").write_text(json.dumps(phases))
    with pytest.raises(ValueError, match="preserved trainable-timeout"):
        retry_active.failed_parent(directory)


@pytest.fixture
def linked(failed):
    parent, phases = failed
    directory = parent / "child"
    (directory / "active-comparison").mkdir(parents=True)
    request = dict(schema="FairFinalActiveResumeRetry/v1", files={
        "active-comparison/reference.json": dict(sha256="a" * 64, bytes=12)},
        source="/source.pt", data_root="/data", device_index=1, timeout_seconds=28800, **GATES)
    publish(directory / "retry-input.json", request)
    process = dict(pid=50, start_ticks="60")
    launch = dict(remote="/new-attempt", retry_input_sha256=sha(directory / "retry-input.json"),
        process=dict(pid=49, start_ticks="59"), **GATES)
    publish(directory / "phase-results.json", [dict(phase="trainable", exit_code=0, process=process)])
    completion = dict(exit_code=0, status="completed_retry_pending_independent_verification",
        retry_input_sha256=launch["retry_input_sha256"], process=process,
        command=["/home/zhichun/firstlight_CR/.venv/bin/python", "-B", "-u",
            "/new-attempt/probe_active_resume.py", "--root", "/new-attempt", "--data-root", "/data",
            "--source", "/source.pt", "--mode", "trainable", "--reference-sha", "a" * 64],
        environment=dict(CUDA_VISIBLE_DEVICES="1", PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1", CUBLAS_WORKSPACE_CONFIG=":4096:8"), **GATES)
    publish(directory / "completion.json", completion)
    for name in ("reference", "frozen", "trainable"):
        identity = process if name == "trainable" else next(item["process"] for item in phases if item["phase"] == name)
        publish(directory / ("active-comparison/" + name + ".json"), dict(process=identity))
    return directory, dict(files=deepcopy(request["files"])), launch, request, phases


def check(linked):
    return verify_active_retry.execution_link(*linked)


def test_new_process_links_to_original_failure_and_preserved_reference(linked):
    assert check(linked) == dict(pid=50, start_ticks="60")


@pytest.mark.parametrize("change", ("failure", "input", "exit", "process", "report", "command", "environment", "scope", "original_process"))
def test_resealed_execution_claims_are_checked_against_original_chain(linked, change):
    directory, receipt, launch, request, phases = linked
    completion = read(directory / "completion.json")
    if change == "failure":
        receipt["files"]["supervisor-failure.json"] = dict(sha256="b" * 64)
    elif change == "input":
        receipt["files"]["active-comparison/reference.json"]["sha256"] = "b" * 64
    elif change == "exit":
        completion["exit_code"] = 124
    elif change == "process":
        completion["process"] = phases[-1]["process"]
    elif change in ("report", "original_process"):
        name = "trainable" if change == "report" else "reference"
        (directory / ("active-comparison/" + name + ".json")).write_text(json.dumps(dict(process=launch["process"])))
    elif change == "command":
        completion["command"][-1] = "b" * 64
    elif change == "environment":
        completion["environment"]["CUDA_VISIBLE_DEVICES"] = "0"
    else:
        completion["training_admissible"] = True
    (directory / "completion.json").write_text(json.dumps(completion))
    with pytest.raises(ValueError):
        check(linked)


def test_retry_command_only_restores_trainable_and_pins_reference(linked):
    directory, _, _, request, _ = linked
    command = retry_runtime.command(directory, request)
    assert command[command.index("--mode") + 1] == "trainable"
    assert command[-1] == "a" * 64
    assert "--reference-sha" in command and "--max-updates" not in command


@pytest.mark.parametrize("device,budget", ((-1, 28800), (8, 28800), (1, 1799), (1, 86401), (True, 28800)))
def test_invalid_budget_and_gpu_rejected_before_archive_or_ssh(tmp_path, device, budget):
    with pytest.raises(ValueError):
        retry_active.request_for(tmp_path, launch_sha="a" * 64, receipt_sha="b" * 64,
            device_index=device, timeout_seconds=budget)
