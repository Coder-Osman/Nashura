from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
import torch

import verify_parent
import retry_selection as retry
from run_selection import GATES, SELECTION, pinned, publish, read, sha
from verify_retry import terminal_content
from test_selection_execution import execution, ENTRY, selection_report
from test_series_training import series, WEIGHTS
from test_round_selection import create
from round_selection import SelectionConfig, implementation_sha, resume_round
from verify_native_resume import checkpoint_state
from native_runner.training.fair_search.contracts import semantic_digest


@pytest.fixture(scope="module")
def checkpoints(series, tmp_path_factory):
    root = tmp_path_factory.mktemp("timeout-checkpoints")
    config = SelectionConfig(patience=20, min_delta=1e6)
    run = create(series, config=config)
    for _ in range(7):
        run.advance(series["initial"])
    original = root / "original.pt"
    run.save(original)
    for _ in range(2):
        run.advance(series["initial"])
    timeout = root / "timeout.pt"
    run.save(timeout)
    while run.terminal is None:
        run.advance(series["initial"])
    terminal = root / "terminal.pt"
    run.save(terminal)
    request = dict(parent_checkpoint_sha256=sha(original),
        parent_checkpoint_state=checkpoint_state(original, sha(original))[0],
        implementation_sha=implementation_sha(), config=vars(config), loss_weights=vars(WEIGHTS), series_sha="e" * 64)
    return dict(original=original, timeout=timeout, terminal=terminal, request=request, config=config)


def content(checkpoints, series, path=None):
    return verify_parent.checkpoint_content(path or checkpoints["timeout"],
        expected_sha=sha(path or checkpoints["timeout"]), original_path=checkpoints["original"],
        request=checkpoints["request"], dataset=series["initial"],
        teacher_sha=checkpoints["request"]["parent_checkpoint_state"]["teacher"])


def test_timeout_cursor_and_full_state_resume_match_continuous_terminal(checkpoints, series, tmp_path):
    result = content(checkpoints, series)
    assert result["state"]["steps"] == 9 and result["offset"] > 0
    resumed = resume_round(checkpoints["timeout"], expected_sha=sha(checkpoints["timeout"]),
        dataset=series["initial"], weights=WEIGHTS, config=checkpoints["config"], bundle=series["bundle"])
    while resumed.terminal is None:
        resumed.advance(series["initial"])
    path = tmp_path / "resumed.pt"
    resumed.save(path)
    expected = torch.load(checkpoints["terminal"], weights_only=True)
    assert semantic_digest(torch.load(path, weights_only=True)) == semantic_digest(expected)
    result = terminal_content(path, selection_report(path, expected["payload"], series["initial"]),
        timeout_path=checkpoints["timeout"], original_path=checkpoints["original"],
        request=checkpoints["request"], dataset=series["initial"], native_schedule=False)
    assert result["timeout_optimizer_steps"] == 9 and result["latest_optimizer_steps"] == 24


@pytest.mark.parametrize("change", ("consumed", "order", "best_optimizer", "best_cursor", "teacher"))
def test_resealed_timeout_corruption_rejected(checkpoints, series, tmp_path, change):
    envelope = deepcopy(torch.load(checkpoints["timeout"], weights_only=True))
    payload = envelope["payload"]
    latest, best = payload["latest"]["payload"], payload["best"]["payload"]
    if change == "consumed":
        latest["cursor"]["consumed"] += 1
    elif change == "order":
        latest["cursor"]["order"].reverse()
    elif change == "best_optimizer":
        best["optimizer"]["param_groups"][0]["lr"] *= 2
    elif change == "best_cursor":
        best["cursor"]["consumed"] += 1
    else:
        latest["teacher_network_sha"] = "0" * 64
    for name in ("latest", "best"):
        payload[name]["sha256"] = semantic_digest(payload[name]["payload"])
    envelope["sha256"] = semantic_digest(payload)
    path = tmp_path / "corrupt.pt"
    torch.save(envelope, path)
    with pytest.raises(ValueError):
        content(checkpoints, series, path)


@pytest.fixture
def retry_fixture(execution, tmp_path, monkeypatch):
    parent = tmp_path / "parent" / "selection"
    parent.mkdir(parents=True)
    inputs, deployment = parent / "inputs", parent / "deployment"
    inputs.mkdir()
    for source in execution["handoff"].iterdir():
        (inputs / source.name).write_bytes(source.read_bytes())
    for name, data in execution["files"].items():
        destination = deployment / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    entry = deployment / SELECTION / "train_round.py"
    entry.write_text(ENTRY.replace('request["parent_checkpoint_sha256"]',
        'json.loads((root / "retry-input.json").read_text())["verification"]["checkpoint_sha256"]')
        .replace('root / "inputs/parent-checkpoint.pt"', 'root / "resume-input.pt"'))
    (parent / "selected.pt").write_bytes(b"timeout-full-state")
    for name in ("failure.json", "execution.json", "status.json"):
        publish(parent / name, dict(status="fixture"))
    root = tmp_path / "retry"
    root.mkdir()
    request = dict(schema="FairFinalSelectionTimeoutRetry/v1", timeout_seconds=1800,
        parent_remote=str(parent), remote=str(root), parent_processes=[dict(pid=100, start_ticks="1")] * 2,
        verification=dict(status="passed_timeout_archive_and_checkpoint_content_only",
            live_resume_verified=False, checkpoint_sha256=sha(parent / "selected.pt"), **GATES),
        files=retry.safe_tree_hash(parent), runtime_sha256={}, **GATES)
    config = dict(remote_root=str(parent.parent), remote_base=str(tmp_path), gpu_uuid="fixture-gpu",
        source=str(execution["options"]["source"]), data_root=str(execution["options"]["data_root"]))
    publish(root / "parent-remote-config.json", config)
    (root / "retry_selection.py").write_bytes(Path(retry.__file__).read_bytes())
    request["runtime_sha256"] = {name: sha(root / name) for name in ("parent-remote-config.json", "retry_selection.py")}
    publish(root / "request.json", request)
    monkeypatch.setattr(retry, "process_matches", lambda identity: False)
    monkeypatch.setattr(retry, "resource_preflight", lambda config: dict(status="fixture_only"))
    monkeypatch.setattr(retry, "acquire_gpu_lock", lambda *args: (os.open(os.devnull, os.O_RDONLY), None))
    return root, parent, request


def test_real_retry_child_uses_timeout_resume_preserves_parent_and_frozen_files(retry_fixture):
    root, parent, request = retry_fixture
    before = retry.safe_tree_hash(parent)
    retry.execute(root, request_sha=sha(root / "request.json"))
    assert retry.safe_tree_hash(parent) == before
    completion = read(root / "selection/completion.json")
    assert completion["status"] == "completed_pending_independent_content_verification"
    assert completion["training_admissible"] is False and completion["exit_code"] == 0
    pinned(root / "selection/resume-input.pt", request["verification"]["checkpoint_sha256"])
    assert (root / "selection/selected.pt").read_bytes() != (parent / "selected.pt").read_bytes()


@pytest.mark.parametrize("change", ("budget", "gate", "proof_gate", "traversal", "process"))
def test_retry_request_rejects_resealed_ineligible_inputs(retry_fixture, change):
    _, _, request = retry_fixture
    if change == "budget":
        request["timeout_seconds"] = 86401
    elif change == "gate":
        request["gates_closed"] = True
    elif change == "proof_gate":
        request["verification"]["formal_g2_pass"] = True
    elif change == "traversal":
        request["files"]["../outside"] = dict(sha256="0" * 64, bytes=1)
    else:
        request["parent_processes"][0]["start_ticks"] = None
    with pytest.raises(ValueError):
        retry.validate(request)


@pytest.mark.parametrize("change", ("bytes", "symlink"))
def test_retry_rejects_changed_parent_before_output(retry_fixture, tmp_path, change):
    root, parent, request = retry_fixture
    if change == "bytes":
        (parent / "selected.pt").write_bytes(b"changed")
    else:
        target = tmp_path / "external"
        (parent / "inputs").rename(target)
        (parent / "inputs").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        retry.prepare_output(parent, root / "selection", request)
    assert not (root / "selection").exists()


def test_retry_real_child_failure_is_quarantined(retry_fixture):
    root, parent, request = retry_fixture
    entry = parent / "deployment" / SELECTION / "train_round.py"
    entry.write_text("raise SystemExit(9)\n")
    request["files"] = retry.safe_tree_hash(parent)
    (root / "request.json").write_text(json.dumps(request))
    with pytest.raises(RuntimeError, match="exit code: 9"):
        retry.execute(root, request_sha=sha(root / "request.json"))
    assert read(root / "failure.json")["status"] == "failed_quarantined"
    assert not (root / "selection/completion.json").exists()


def test_inventory_waits_for_training_child_and_gpu_lease(retry_fixture, monkeypatch):
    root, _, request = retry_fixture
    child = root / "selection"
    child.mkdir()
    publish(child / "status.json", dict(process=dict(pid=200, start_ticks="2")))
    monkeypatch.setattr(retry, "process_matches", lambda identity: identity.get("pid") == 200)
    result = retry.inventory(root, dict(pid=100, start_ticks="1"), sha(root / "request.json"))
    assert result["running"] == ["training_child"] and result["files"] == {}
    monkeypatch.setattr(retry, "process_matches", lambda identity: False)
    def locked(*args):
        raise RuntimeError("lease held")
    monkeypatch.setattr(retry, "acquire_gpu_lock", locked)
    result = retry.inventory(root, dict(pid=100, start_ticks="1"), sha(root / "request.json"))
    assert result["running"] == ["gpu_lease"] and result["files"] == {}


def test_collect_stopped_archive_is_verified_and_idempotent(retry_fixture, tmp_path, monkeypatch):
    import run_retry
    remote, parent, request = retry_fixture
    retry.execute(remote, request_sha=sha(remote / "request.json"))
    process = dict(pid=100, start_ticks="1")
    publish(remote / "worker-process.json", process)
    local = tmp_path / "local-attempt"
    local.mkdir()
    for name in ("request.json", *request["runtime_sha256"]):
        shutil.copyfile(remote / name, local / name)
    publish(local / "preparation.json", dict(parent_local=str(parent.parent)))
    publish(local / "launch.json", dict(remote=str(remote), process=process,
        request_sha256=sha(local / "request.json"), preparation_sha256=sha(local / "preparation.json")))
    class Sftp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, source, destination):
            shutil.copyfile(source, destination)
    class Client:
        def open_sftp(self):
            return Sftp()
        def close(self):
            pass
    monkeypatch.setattr(run_retry, "connect", Client)
    monkeypatch.setattr(run_retry, "execute", lambda *args: json.dumps(
        retry.inventory(remote, process, sha(remote / "request.json"))))
    result = run_retry.collect(local, launch_sha=sha(local / "launch.json"))
    assert result["status"] == "terminal_archive_ready_for_independent_verification"
    assert run_retry.collect(local, launch_sha=sha(local / "launch.json")) == result
    assert retry.safe_tree_hash(local / "archive") == retry.safe_tree_hash(remote)
    (local / "archive/selection/selected.pt").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="pinned"):
        run_retry.collect(local, launch_sha=sha(local / "launch.json"))
