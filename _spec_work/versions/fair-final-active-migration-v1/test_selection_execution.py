from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time

import pytest
import torch

from test_active_verifier import CONFIG, comparison, series
from test_series_training import WEIGHTS
from test_round_selection import create
from round_selection import SelectionConfig, implementation_sha, resume_round
from verify_native_resume import checkpoint_state
from native_runner.training.fair_search.contracts import semantic_digest
import run_selection as worker
import verify_selection_execution as verifier


ENTRY = '''import hashlib
import json
import os
from pathlib import Path
import sys

args = sys.argv
root = Path.cwd()
request = json.loads((root / "inputs/selection-handoff.json").read_text())
assert "--max-updates" not in args and "--resume-sha" in args
assert args[args.index("--resume-sha") + 1] == request["parent_checkpoint_sha256"]
assert os.environ["CUBLAS_WORKSPACE_CONFIG"] == ":4096:8"
assert all(Path(item).is_relative_to(root / "deployment") for item in os.environ["PYTHONPATH"].split(os.pathsep))
assert (root / "selected.pt").read_bytes() == (root / "inputs/parent-checkpoint.pt").read_bytes()
(root / "selected.pt").write_bytes(b"terminal-execution-fixture")
report = dict(schema="FairFinalMigrationSelectionReport/v1", terminal="EARLY_STOP",
    checkpoint_sha=hashlib.sha256((root / "selected.pt").read_bytes()).hexdigest(),
    implementation_sha=request["implementation_sha"], dataset_identity=request["dataset_identity"],
    series_sha=request["series_sha"], config=request["config"], weights=request["loss_weights"],
    latest_optimizer_steps=8, training_admissible=False, formal_g2_pass=False)
(root / "selected.report.json").write_text(json.dumps(report))
'''


def package(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def write_request(fixture):
    request = fixture["request"]
    for name in worker.INPUTS:
        path = fixture["handoff"] / name
        request["files"][name] = dict(sha256=worker.sha(path), bytes=path.stat().st_size)
    (fixture["handoff"] / "selection-handoff.json").write_text(json.dumps(request))
    fixture["options"]["expected_sha"] = worker.sha(fixture["handoff"] / "selection-handoff.json")


@pytest.fixture
def execution(tmp_path):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    source = tmp_path / "source.pt"
    source.write_bytes(b"source-policy-fixture")
    data = tmp_path / "data"
    data.mkdir()
    identity = dict(source_a_sha=worker.sha(source))
    request = dict(schema="FairFinalActiveSelectionHandoff/v1", status="prepared_only_not_executed",
        resume_optimizer_rng_cursor_history=True, max_updates=None,
        parent_checkpoint_state=dict(steps=7), split_rows=dict(train=352, validation=358, test=334),
        files={}, dataset_identity=identity, series_sha="e" * 64, implementation_sha="f" * 64,
        handoff_sha="a" * 64, parent_launch_sha256="b" * 64, parent_receipt_sha256="c" * 64,
        config=dict(patience=2, min_delta=1e-6), loss_weights=dict(conditional_policy_kl=1.), **worker.GATES)
    proof = dict(status="passed_active_archive_and_two_stage_checkpoint_scope",
        content=dict(native_schedule_verified=True, branches=dict(trainable=dict(state=dict(steps=7)))),
        frozen_deployment_verified=True, dataset_identity=identity, split_rows=request["split_rows"],
        series_sha=request["series_sha"], launch_sha256=request["parent_launch_sha256"],
        receipt_sha256=request["parent_receipt_sha256"], **worker.GATES)
    (handoff / "parent-verification.json").write_text(json.dumps(proof))
    (handoff / "parent-checkpoint.pt").write_bytes(b"full-parent-execution-fixture")
    request["parent_checkpoint_sha256"] = worker.sha(handoff / "parent-checkpoint.pt")
    files = {worker.SELECTION + "/train_round.py": ENTRY.encode(),
        worker.SERIES + "/deterministic_runtime.py": b"FROZEN = True\n",
        "private-plan.json": json.dumps(dict(rounds=[dict(manifest=dict(identity=identity))])).encode(),
        "selection-config.json": json.dumps(request["config"]).encode(),
        "loss-weights.json": json.dumps(request["loss_weights"]).encode()}
    manifest = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    files["deployment.json"] = json.dumps(manifest).encode()
    (handoff / "payload.tar.gz").write_bytes(package(files.items()))
    fixture = dict(handoff=handoff, request=request, files=files,
        options=dict(output=tmp_path / "execution", source=source, data_root=data, device="cpu"))
    write_request(fixture)
    return fixture


def execute(fixture):
    return worker.execute(fixture["handoff"], **fixture["options"])


def test_real_child_uses_frozen_deployment_full_resume_and_independent_output(execution):
    before = {path.name: path.read_bytes() for path in execution["handoff"].iterdir()}
    completion = execute(execution)
    output = execution["options"]["output"]
    assert completion["status"] == "completed_pending_independent_content_verification"
    assert completion["exit_code"] == 0 and completion["training_admissible"] is False
    assert completion["process"]["pid"] != os.getpid()
    assert {path.name: path.read_bytes() for path in execution["handoff"].iterdir()} == before
    assert (output / "inputs/parent-checkpoint.pt").read_bytes() == before["parent-checkpoint.pt"]
    assert all((output / "deployment" / name).read_bytes() == content
        for name, content in execution["files"].items())
    assert worker.read(output / "completion.json") == completion
    assert not (output / "failure.json").exists()
    with pytest.raises(ValueError, match="new independent"):
        execute(execution)


@pytest.mark.parametrize("name", ("selection-handoff.json", *worker.INPUTS))
def test_modified_input_rejected_before_output(execution, name):
    path = execution["handoff"] / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="pinned"):
        execute(execution)
    assert not execution["options"]["output"].exists()


@pytest.mark.parametrize("field,value", (("training_admissible", True), ("max_updates", 1),
    ("status", "completed"), ("resume_optimizer_rng_cursor_history", False)))
def test_resealed_ineligible_handoff_rejected(execution, field, value):
    execution["request"][field] = value
    write_request(execution)
    with pytest.raises(ValueError, match="verified native"):
        execute(execution)


@pytest.mark.parametrize("name", ("../outside.py", "/absolute.py", "./ambiguous.py", "duplicate"))
def test_resealed_unsafe_archive_rejected(execution, name):
    entries = list(execution["files"].items())
    entries.append(entries[0] if name == "duplicate" else (name, b"pass\n"))
    (execution["handoff"] / "payload.tar.gz").write_bytes(package(entries))
    write_request(execution)
    with pytest.raises(ValueError, match="member"):
        execute(execution)
    assert not execution["options"]["output"].exists()


@pytest.mark.parametrize("change", ("source", "proof", "manifest", "config"))
def test_resealed_wrong_content_rejected(execution, change):
    if change == "source":
        execution["options"]["source"].write_bytes(b"changed")
    elif change == "proof":
        path = execution["handoff"] / "parent-verification.json"
        proof = worker.read(path)
        proof["content"]["native_schedule_verified"] = False
        path.write_text(json.dumps(proof))
    else:
        files = dict(execution["files"])
        files["selection-config.json"] = b"{}"
        if change == "config":
            files["deployment.json"] = json.dumps({name: hashlib.sha256(content).hexdigest()
                for name, content in files.items() if name != "deployment.json"}).encode()
        (execution["handoff"] / "payload.tar.gz").write_bytes(package(files.items()))
    write_request(execution)
    with pytest.raises(ValueError):
        execute(execution)
    assert not execution["options"]["output"].exists()


def test_nonzero_real_child_is_quarantined_without_completion(execution):
    files = execution["files"]
    files[worker.SELECTION + "/train_round.py"] = b"raise SystemExit(9)\n"
    files["deployment.json"] = json.dumps({name: hashlib.sha256(content).hexdigest()
        for name, content in files.items() if name != "deployment.json"}).encode()
    (execution["handoff"] / "payload.tar.gz").write_bytes(package(files.items()))
    write_request(execution)
    with pytest.raises(RuntimeError, match="exit code: 9"):
        execute(execution)
    output = execution["options"]["output"]
    assert worker.read(output / "failure.json")["status"] == "failed_quarantined"
    assert not (output / "completion.json").exists()


def test_real_timeout_kills_term_ignoring_process_group(tmp_path):
    program = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    code, process = worker.run_child([sys.executable, "-c", program], root=tmp_path,
        environment={}, timeout_seconds=.2, grace_seconds=.05)
    assert code == 124
    with pytest.raises(ProcessLookupError):
        os.kill(process["pid"], 0)


def test_cleanup_includes_descendant_after_leader_exits(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    descendant = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    program = ("import pathlib,subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c',"
        + repr(descendant) + "]); pathlib.Path(" + repr(str(pidfile))
        + ").write_text(str(child.pid)); time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", program], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pidfile.exists()
        time.sleep(.05)
        os.kill(child.pid, signal.SIGTERM)
        child.wait(timeout=2)
        worker.stop_child(child, .05)
        descendant_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            stat = Path("/proc", str(descendant_pid), "stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
        else:
            pytest.fail("descendant survived process-group cleanup")
    finally:
        worker.stop_child(child, .05)


def selection_report(path, payload, dataset):
    return dict(schema="FairFinalMigrationSelectionReport/v1", checkpoint_sha=worker.sha(path),
        implementation_sha=payload["implementation_sha"], config=payload["config"], weights=vars(WEIGHTS),
        dataset_identity=dataset.identity, series_sha="e" * 64, validation_sha=payload["validation_sha"],
        terminal=payload["terminal"], latest_optimizer_steps=payload["latest"]["payload"]["cursor"]["optimizer_steps"],
        selected=payload["history"][payload["best_index"]], history=payload["history"],
        network_sha=semantic_digest(payload["best"]["payload"]["network"]),
        training_admissible=False, formal_g2_pass=False)


@pytest.fixture(scope="module")
def terminal(comparison, series, tmp_path_factory):
    directory = tmp_path_factory.mktemp("terminal-selection")
    parent = comparison / "trainable-reference.pt"
    run = resume_round(parent, expected_sha=worker.sha(parent), dataset=series["initial"],
        weights=WEIGHTS, config=CONFIG, bundle=series["bundle"])
    while run.terminal is None:
        run.advance(series["initial"])
    path = directory / "terminal.pt"
    run.save(path)
    request = dict(parent_checkpoint_sha256=worker.sha(parent),
        parent_checkpoint_state=checkpoint_state(parent, worker.sha(parent))[0],
        implementation_sha=implementation_sha(), config=vars(CONFIG), loss_weights=vars(WEIGHTS), series_sha="e" * 64)
    return dict(path=path, parent=parent, request=request)


def check_terminal(terminal, series, *, path=None, native_schedule=False):
    path = path or terminal["path"]
    payload = torch.load(path, weights_only=True)["payload"]
    return verifier.verify_terminal(path, selection_report(path, payload, series["initial"]),
        parent_path=terminal["parent"], request=terminal["request"], dataset=series["initial"],
        native_schedule=native_schedule)


def test_real_small_model_reaches_terminal_preserving_best_full_state(terminal, series):
    result = check_terminal(terminal, series)
    assert result["terminal"] == "EARLY_STOP" and result["latest_optimizer_steps"] == 8
    assert result["native_schedule_verified"] is False and result["parent_history_prefix_verified"] is True
    with pytest.raises(ValueError, match="native terminal selection batch"):
        check_terminal(terminal, series, native_schedule=True)


@pytest.mark.parametrize("change,error", (("order", "sampler"), ("consumed", "accounting"),
    ("teacher", "terminal continuation"), ("best", "original trainer")))
def test_resealed_terminal_state_corruption_rejected(terminal, series, tmp_path, change, error):
    envelope = deepcopy(torch.load(terminal["path"], weights_only=True))
    payload = envelope["payload"]
    if change == "order":
        payload["latest"]["payload"]["cursor"]["order"].reverse()
    elif change == "consumed":
        payload["latest"]["payload"]["cursor"]["consumed"] += 1
    elif change == "teacher":
        payload["latest"]["payload"]["teacher_network_sha"] = "0" * 64
    else:
        payload["best"]["payload"]["optimizer"]["param_groups"][0]["lr"] *= 2
    for name in ("latest", "best"):
        payload[name]["sha256"] = semantic_digest(payload[name]["payload"])
    envelope["sha256"] = semantic_digest(payload)
    path = tmp_path / "changed.pt"
    torch.save(envelope, path)
    with pytest.raises(ValueError, match=error):
        check_terminal(terminal, series, path=path)


def test_real_max_epoch_cursor_does_not_shuffle_a_thirteenth_time(series, tmp_path):
    config = SelectionConfig(patience=20, min_delta=1e6)
    run = create(series, config=config)
    for _ in range(7):
        run.advance(series["initial"])
    parent = tmp_path / "parent.pt"
    run.save(parent)
    request = dict(parent_checkpoint_sha256=worker.sha(parent),
        parent_checkpoint_state=checkpoint_state(parent, worker.sha(parent))[0],
        implementation_sha=implementation_sha(), config=vars(config), loss_weights=vars(WEIGHTS), series_sha="e" * 64)
    while run.terminal is None:
        run.advance(series["initial"])
    path = tmp_path / "terminal.pt"
    run.save(path)
    result = check_terminal(dict(path=path, parent=parent, request=request), series)
    assert result["terminal"] == "MAX_EPOCHS" and result["latest_optimizer_steps"] == 24
