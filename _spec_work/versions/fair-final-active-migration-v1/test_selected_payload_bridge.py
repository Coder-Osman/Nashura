from copy import deepcopy
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from test_selection_execution import execution, execute
import verify_selection_execution as verifier

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-migration-integration-v1"))
from build_collection_payload import build_selected_collection, INTEGRATION


def options(execution):
    directory = execution["options"]["output"]
    digest = lambda name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
    return dict(execution_sha=digest("execution.json"), completion_sha=digest("completion.json"),
        data_roots=[execution["options"]["data_root"]])


def proof_for(arguments):
    return dict(status="passed_independent_terminal_selection_content_scope",
        execution_sha256=arguments["execution_sha"], completion_sha256=arguments["completion_sha"],
        frozen_deployment_verified=True, native_game_provenance_reverified=False,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)


def test_bridge_requires_real_content_verification_before_packaging(execution):
    execute(execution)
    with pytest.raises((ValueError, KeyError)):
        build_selected_collection(execution["options"]["output"], **options(execution))


def test_verified_bridge_preserves_training_bytes_and_records_parent(execution, monkeypatch):
    execute(execution)
    arguments = options(execution)
    proof = proof_for(arguments)
    calls = []

    def verify(directory, **supplied):
        calls.append((directory, supplied))
        return deepcopy(proof)

    monkeypatch.setattr(verifier, "verify", verify)
    parent = execution["options"]["output"]
    package, report = build_selected_collection(parent, **arguments)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {entry.name: archive.extractfile(entry).read() for entry in archive.getmembers()}
    assert calls == [(parent, arguments)]
    for name, data in execution["files"].items():
        if name != "deployment.json":
            assert files[name] == data
    assert INTEGRATION + "collect_selected.py" in files
    assert report["schema"] == "FairFinalSelectedCollectionPayload/v1"
    assert report["selection_parent"]["verification"] == proof
    assert report["selection_parent"]["checkpoint_sha256"] == hashlib.sha256(
        (parent / "selected.pt").read_bytes()).hexdigest()
    assert report["native_collection_executed"] is report["training_admissible"] is False
    assert report["requires_terminal_selected_parent"] is report["requires_registered_tasks"] is True
    assert build_selected_collection(parent, **arguments) == (package, report)


@pytest.mark.parametrize("field,value", [("status", "passed_preflight_only"),
    ("frozen_deployment_verified", False), ("training_admissible", True),
    ("formal_g2_pass", True), ("gates_closed", True),
    ("execution_sha256", "0" * 64), ("completion_sha256", "0" * 64)])
def test_wrong_verification_scope_or_parent_rejected(execution, monkeypatch, field, value):
    execute(execution)
    arguments = options(execution)
    proof = proof_for(arguments)
    proof[field] = value
    monkeypatch.setattr(verifier, "verify", lambda *unused, **kwargs: proof)
    with pytest.raises(ValueError, match="independently verified"):
        build_selected_collection(execution["options"]["output"], **arguments)


@pytest.mark.parametrize("filename", ["execution.json", "completion.json", "selected.pt",
    "inputs/payload.tar.gz", "inputs/selection-handoff.json"])
def test_changed_inputs_after_verification_rejected(execution, monkeypatch, filename):
    execute(execution)
    arguments = options(execution)
    parent = execution["options"]["output"]

    def verify(*unused, **kwargs):
        path = parent / filename
        path.write_bytes(path.read_bytes() + b" ")
        return proof_for(arguments)

    monkeypatch.setattr(verifier, "verify", verify)
    with pytest.raises(ValueError):
        build_selected_collection(parent, **arguments)


@pytest.mark.parametrize("arguments", [[], ["--selection-completion-sha", "a" * 64],
    ["--selection-execution-sha", "a" * 64],
    ["--parent-launch-sha", "a" * 64, "--selection-completion-sha", "b" * 64],
    ["--selection-execution-sha", "a" * 64, "--selection-completion-sha", "b" * 64,
        "--selection-data-roots", "data", "--runtime-parent", "ancestor"]])
def test_cli_requires_complete_unambiguous_parent_before_output(tmp_path, arguments):
    target = tmp_path / "output"
    command = [sys.executable, str(ROOT.parent / "fair-final-migration-integration-v1/build_collection_payload.py"),
        str(tmp_path / "parent"), "--output", str(target), *arguments]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert not target.exists()
