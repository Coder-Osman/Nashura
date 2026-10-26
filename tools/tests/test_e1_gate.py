import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e1_gate import evaluate, RESOURCE_IMPORTS, SEMANTIC_CHECKS, DESTROY_LABELS


def evidence():
    from verify_e1_gate import EXPECTED_TRAJECTORY
    return [dict(schema="e0-snapshot-comparison.v1", status="passed", counts_sha256="fixture", **EXPECTED_TRAJECTORY),
        dict(schema="e0-dependency-counts.v1", engine_loaded=True,
             counts=[dict(labels=[name], count=1, phase="destroy", first_sequence=index,
                          host_phase="terminal_reset_destroy", native_scopes=[name])
                     for index, name in enumerate(sorted(DESTROY_LABELS))],
             coverage=[dict(label=name, status="attached") for name in RESOURCE_IMPORTS],
             errors=[], scope="synthetic engineering fixture"),
        dict(receipt=dict(installed=True, errors=[], redirects=[dict(name=name, verified=True)
             for name in RESOURCE_IMPORTS]), metrics=dict(failures="0", live_assets="0", entries="9829", calls="1")),
        dict(schema="e1-semantics.v2")]


@pytest.fixture(autouse=True)
def isolated_semantics(monkeypatch):
    monkeypatch.setattr("verify_e1_gate.verify_semantics", lambda manifest, attachments:
        (dict.fromkeys(SEMANTIC_CHECKS, manifest.get("fixture_pass", True)), {}))


def run(inputs):
    return evaluate(*inputs, counts_sha256="fixture")


def test_fixture_gate_does_not_claim_vm_or_training():
    result = run(evidence())
    assert result["status"] == "passed"
    assert not result["vm_framework_free_verified"]
    assert not result["training_admissible"]
    assert not result["tenfold_speedup_verified"]


def test_failed_attachment_validation_blocks():
    inputs = evidence()
    inputs[3]["fixture_pass"] = False
    assert run(inputs)["status"] == "blocked"


def test_fmod_or_any_caller_remaining_is_not_filtered_and_aliases_not_added():
    inputs = evidence()
    inputs[1]["counts"] = [dict(labels=["AAssetManager_fromJava", "AAssetAlias"],
        count=1, phase="load", first_sequence=0, caller="synthetic_other_library")]
    result = run(inputs)
    assert result["status"] == "blocked"
    assert result["actual_assetmanager_events"] == 1


@pytest.mark.parametrize("mutation", ["coverage", "hook", "redirect", "failure", "leak", "trajectory"])
def test_fail_closed(mutation):
    inputs = evidence()
    if mutation == "coverage":
        inputs[1]["coverage"].pop()
    elif mutation == "hook":
        inputs[1]["coverage"][0]["status"] = "failed"
    elif mutation == "redirect":
        inputs[2]["receipt"]["redirects"][0]["verified"] = False
    elif mutation in ("failure", "leak"):
        inputs[2]["metrics"]["failures" if mutation == "failure" else "live_assets"] = "1"
    else:
        inputs[0]["status"] = "failed"
    original = copy.deepcopy(inputs)
    assert run(inputs)["status"] == "blocked"
    assert inputs == original


@pytest.mark.parametrize("field", ["snapshot_count", "terminal_tick", "injected_actions", "total_bytes", "trajectory_sha256"])
def test_partial_trajectory_rejected(field):
    inputs = evidence()
    inputs[0][field] = 3
    assert run(inputs)["status"] == "blocked"


def test_counts_must_be_bound_to_exact_bytes():
    assert evaluate(*evidence(), counts_sha256="different")["status"] == "blocked"
    assert evaluate(*evidence())["status"] == "blocked"


def test_final_reset_alone_does_not_prove_battle_destruction():
    inputs = evidence()
    inputs[1]["counts"][0]["native_scopes"] = []
    assert "terminal battle destruction coverage missing" in run(inputs)["reasons"]


def test_malformed_input_writes_blocked_receipt_and_preserves_output(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{")
    output = tmp_path / "receipt.json"
    command = [sys.executable, str(Path(__file__).parents[1] / "verify_e1_gate.py")]
    for name in ("comparison", "counts", "redirect", "semantics"):
        command.extend(["--" + name, str(invalid)])
    command.extend(["--output", str(output)])
    assert subprocess.run(command, capture_output=True).returncode == 2
    receipt = output.read_bytes()
    assert json.loads(receipt)["status"] == "blocked"
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_bytes() == receipt
