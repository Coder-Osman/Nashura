from copy import deepcopy
import io
from pathlib import Path
import shutil
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / name) for name in (
    "fair-final-selection-timeout-v1", "fair-final-active-migration-v1",
    "fair-final-migration-selection-v1", "fair-final-migration-series-v1")]
sys.path.insert(0, str(ROOT.parents[2] / "firstlight_CR"))

import prepare_collection as bridge
import retry_selection as retry
from test_timeout import retry_fixture
from test_selection_execution import execution
from run_selection import GATES, publish, read, sha
from build_collection_payload import INTEGRATION


@pytest.fixture
def terminal(retry_fixture, tmp_path):
    remote, original, request = retry_fixture
    retry.execute(remote, request_sha=sha(remote / "request.json"))
    parent = tmp_path / "received-retry"
    parent.mkdir()
    archive = parent / "archive"
    shutil.copytree(remote, archive)
    launch = dict(schema="FairFinalSelectionTimeoutLaunch/v1", process=dict(pid=100, start_ticks="1"),
        remote=str(remote), request_sha256=sha(remote / "request.json"), **GATES)
    publish(parent / "launch.json", launch)
    publish(parent / "receipt.json", dict(schema="FairFinalSelectionTimeoutReceipt/v1",
        launch_sha256=sha(parent / "launch.json"),
        status="terminal_archive_ready_for_independent_verification",
        inventory=dict(state="terminal_archive_ready_for_independent_verification",
            running=[], lease_available=True, files=retry.safe_tree_hash(archive), **GATES), **GATES))
    arguments = dict(launch_sha=sha(parent / "launch.json"), receipt_sha=sha(parent / "receipt.json"),
        data_roots=[tmp_path / "fixture-data"])
    proof = dict(status="passed_independent_retry_terminal_content_scope",
        launch_sha256=arguments["launch_sha"], receipt_sha256=arguments["receipt_sha"],
        verifier_sha256=sha(bridge.verify_retry.__file__), native_game_provenance_reverified=False,
        content=dict(native_schedule_verified=True, timeout_history_prefix_verified=True,
            latest_optimizer_steps=24, timeout_optimizer_steps=15), **GATES)
    return dict(parent=parent, original=original, arguments=arguments, proof=proof)


def allow_fixture(terminal, monkeypatch):
    calls = []
    def verify(parent, **arguments):
        calls.append((parent, arguments))
        return deepcopy(terminal["proof"])
    monkeypatch.setattr(bridge.verify_retry, "verify", verify)
    return calls


def test_actual_verifier_rejects_synthetic_terminal_before_output(terminal, tmp_path):
    output = tmp_path / "retry-collection"
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        bridge.prepare(terminal["parent"], output=output, **terminal["arguments"])
    assert not output.exists()


def test_mock_verified_bridge_preserves_frozen_bytes_and_parent(terminal, execution, monkeypatch, tmp_path):
    calls = allow_fixture(terminal, monkeypatch)
    parent = terminal["parent"]
    before = retry.safe_tree_hash(parent)
    original = retry.safe_tree_hash(terminal["original"])
    output = tmp_path / "retry-collection"
    report = bridge.prepare(parent, output=output, **terminal["arguments"])
    assert calls == [(parent, terminal["arguments"])]
    assert retry.safe_tree_hash(parent) == before
    assert retry.safe_tree_hash(terminal["original"]) == original
    assert set(path.name for path in output.iterdir()) == bridge.OUTPUT_FILES | {"handoff.json"}
    assert report["native_collection_executed"] is report["training_admissible"] is False
    with tarfile.open(fileobj=io.BytesIO((output / "payload.tar.gz").read_bytes()), mode="r:gz") as archive:
        files = {entry.name: archive.extractfile(entry).read() for entry in archive.getmembers()}
    for name, data in execution["files"].items():
        if name != "deployment.json":
            assert files[name] == data
    assert INTEGRATION + "collect_selected.py" in files
    assert (output / "selected.pt").read_bytes() == (parent / "archive/selection/selected.pt").read_bytes()
    result = bridge.verify(output, expected_sha=sha(output / "handoff.json"), parent=parent,
        **terminal["arguments"])
    assert result["status"] == "passed_retry_collection_handoff_only"
    assert all(result[name] is value for name, value in GATES.items())
    assert bridge.build(parent, **terminal["arguments"]) == bridge.build(parent, **terminal["arguments"])


@pytest.mark.parametrize("field,value", [("status", "passed_preflight_only"),
    ("launch_sha256", "0" * 64), ("receipt_sha256", "0" * 64),
    ("verifier_sha256", "0" * 64), ("native_game_provenance_reverified", True),
    ("training_admissible", True), ("formal_g2_pass", True), ("gates_closed", True)])
def test_wrong_proof_rejected(terminal, monkeypatch, field, value):
    terminal["proof"][field] = value
    allow_fixture(terminal, monkeypatch)
    with pytest.raises(ValueError, match="independently verified"):
        bridge.build(terminal["parent"], **terminal["arguments"])


@pytest.mark.parametrize("field,value", [("native_schedule_verified", False),
    ("timeout_history_prefix_verified", False), ("latest_optimizer_steps", 15)])
def test_incomplete_content_rejected(terminal, monkeypatch, field, value):
    terminal["proof"]["content"][field] = value
    allow_fixture(terminal, monkeypatch)
    with pytest.raises(ValueError, match="independently verified"):
        bridge.build(terminal["parent"], **terminal["arguments"])


@pytest.mark.parametrize("filename", ["launch.json", "receipt.json", "archive/selection/selected.pt",
    "archive/selection/inputs/payload.tar.gz", "archive/selection/inputs/selection-handoff.json"])
def test_changed_parent_after_verification_rejected(terminal, monkeypatch, filename):
    def verify(*unused, **arguments):
        path = terminal["parent"] / filename
        path.write_bytes(path.read_bytes() + b" ")
        return deepcopy(terminal["proof"])
    monkeypatch.setattr(bridge.verify_retry, "verify", verify)
    with pytest.raises(ValueError):
        bridge.build(terminal["parent"], **terminal["arguments"])


@pytest.mark.parametrize("change", ["running", "lease", "status", "extra", "symlink"])
def test_ineligible_receipt_or_archive_rejected(terminal, monkeypatch, change, tmp_path):
    parent = terminal["parent"]
    receipt = read(parent / "receipt.json")
    if change == "running":
        receipt["inventory"]["running"] = ["training_child"]
    elif change == "lease":
        receipt["inventory"]["lease_available"] = False
    elif change == "status":
        receipt["status"] = "failed_quarantined"
    elif change == "extra":
        (parent / "archive/unreceipted").write_bytes(b"extra")
    else:
        path = parent / "archive/selection/selected.pt"
        target = tmp_path / "external.pt"
        path.rename(target)
        path.symlink_to(target)
    (parent / "receipt.json").write_bytes(bridge.encoded(receipt))
    terminal["arguments"]["receipt_sha"] = sha(parent / "receipt.json")
    terminal["proof"]["receipt_sha256"] = terminal["arguments"]["receipt_sha"]
    allow_fixture(terminal, monkeypatch)
    with pytest.raises(ValueError):
        bridge.build(parent, **terminal["arguments"])


@pytest.mark.parametrize("location", ["child", "ancestor", "existing", "alias"])
def test_output_must_be_new_and_independent(terminal, monkeypatch, tmp_path, location):
    parent = terminal["parent"]
    outputs = dict(child=parent / "new", ancestor=parent.parent, existing=tmp_path / "existing",
        alias=tmp_path / "alias")
    outputs["existing"].mkdir()
    outputs["alias"].symlink_to(parent, target_is_directory=True)
    calls = allow_fixture(terminal, monkeypatch)
    with pytest.raises(ValueError):
        bridge.prepare(parent, output=outputs[location], **terminal["arguments"])
    assert not calls


def test_output_failure_keeps_quarantine_without_completion(terminal, monkeypatch, tmp_path):
    allow_fixture(terminal, monkeypatch)
    original = bridge.write_new
    def interrupted(path, data):
        if path.name == "selected.pt":
            raise OSError("fixture interrupted write")
        original(path, data)
    monkeypatch.setattr(bridge, "write_new", interrupted)
    output = tmp_path / "retry-collection"
    with pytest.raises(OSError, match="interrupted"):
        bridge.prepare(terminal["parent"], output=output, **terminal["arguments"])
    assert read(output / "failure.json")["status"] == "failed_quarantined"
    assert (output / "payload.tar.gz").exists() and not (output / "handoff.json").exists()


@pytest.mark.parametrize("change", ["bytes", "reseal", "gate", "extra", "symlink", "parent"])
def test_handoff_revalidation_rejects_changes(terminal, monkeypatch, tmp_path, change):
    allow_fixture(terminal, monkeypatch)
    output = tmp_path / "retry-collection"
    bridge.prepare(terminal["parent"], output=output, **terminal["arguments"])
    if change in ("bytes", "reseal"):
        (output / "selected.pt").write_bytes(b"changed")
    if change == "reseal":
        report = read(output / "handoff.json")
        report["files"]["selected.pt"] = dict(sha256=sha(output / "selected.pt"), bytes=7)
        (output / "handoff.json").write_bytes(bridge.encoded(report))
    elif change == "gate":
        report = read(output / "handoff.json")
        report["formal_g2_pass"] = True
        (output / "handoff.json").write_bytes(bridge.encoded(report))
    elif change == "extra":
        (output / "failure.json").write_bytes(b"{}")
    elif change == "symlink":
        target = tmp_path / "external.pt"
        (output / "selected.pt").rename(target)
        (output / "selected.pt").symlink_to(target)
    elif change == "parent":
        (terminal["parent"] / "archive/selection/selected.pt").write_bytes(b"changed")
    with pytest.raises(ValueError):
        bridge.verify(output, expected_sha=sha(output / "handoff.json"), parent=terminal["parent"],
            **terminal["arguments"])


def test_second_prepare_keeps_completed_handoff_unchanged(terminal, monkeypatch, tmp_path):
    allow_fixture(terminal, monkeypatch)
    output = tmp_path / "retry-collection"
    bridge.prepare(terminal["parent"], output=output, **terminal["arguments"])
    before = retry.safe_tree_hash(output)
    with pytest.raises(ValueError, match="new collection handoff"):
        bridge.prepare(terminal["parent"], output=output, **terminal["arguments"])
    assert retry.safe_tree_hash(output) == before


def test_running_receipt_stops_before_content_verification(terminal, monkeypatch):
    parent = terminal["parent"]
    receipt = read(parent / "receipt.json")
    receipt["inventory"]["running"] = ["training_child"]
    (parent / "receipt.json").write_bytes(bridge.encoded(receipt))
    terminal["arguments"]["receipt_sha"] = sha(parent / "receipt.json")
    calls = allow_fixture(terminal, monkeypatch)
    with pytest.raises(ValueError, match="stopped terminal"):
        bridge.build(parent, **terminal["arguments"])
    assert not calls


def test_parent_mutation_during_packaging_rejected(terminal, monkeypatch):
    allow_fixture(terminal, monkeypatch)
    original = bridge.build_from_parent
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (terminal["parent"] / "archive/selection/selected.pt").write_bytes(b"changed")
        return result
    monkeypatch.setattr(bridge, "build_from_parent", changed)
    with pytest.raises(ValueError):
        bridge.build(terminal["parent"], **terminal["arguments"])


def test_parent_alias_rejected_before_verification(terminal, monkeypatch, tmp_path):
    alias = tmp_path / "parent-alias"
    alias.symlink_to(terminal["parent"], target_is_directory=True)
    calls = allow_fixture(terminal, monkeypatch)
    with pytest.raises(ValueError, match="symlinks"):
        bridge.build(alias, **terminal["arguments"])
    assert not calls


def test_corrupted_write_quarantined_before_handoff(terminal, monkeypatch, tmp_path):
    allow_fixture(terminal, monkeypatch)
    original = bridge.write_new
    def corrupted(path, data):
        original(path, data + b"corrupted" if path.name == "selected.pt" else data)
    monkeypatch.setattr(bridge, "write_new", corrupted)
    output = tmp_path / "retry-collection"
    with pytest.raises(ValueError, match="file inventory differs"):
        bridge.prepare(terminal["parent"], output=output, **terminal["arguments"])
    assert read(output / "failure.json")["status"] == "failed_quarantined"
    assert not (output / "handoff.json").exists()
