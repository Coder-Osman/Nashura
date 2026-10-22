from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from verify_native_refill import validate


PREFIX = "tick-110-seat-0/"


@pytest.fixture(scope="module")
def attempt_directory():
    configured = os.environ.get("FAIR_V2_NATIVE_ATTEMPT")
    if not configured:
        pytest.skip("set FAIR_V2_NATIVE_ATTEMPT to collected native evidence")
    return Path(configured).resolve()


def test_original_native_evidence_passes(attempt_directory):
    result = validate(attempt_directory)
    assert result["status"] == "passed_limited_scope"
    assert result["valid_simulations"] == 512
    assert result["training_admissible"] is False
    assert result["general_pending_closed"] is False


def mutate_case(case, documents):
    native = documents[PREFIX + "report.json"]
    first = native["records"][0]
    result = first["outcome"]["result"]
    if case == "budget":
        result["valid_simulations"] = 127
    elif case == "unfinished":
        result["unfinished_edge_backups"] = 1
    elif case == "visits":
        result["candidates"][0]["visits"] += 1
    elif case == "noise":
        result["noise_fraction"] = 0.25
    elif case == "viewpoint":
        result["value_perspective"] = 1
    elif case == "sampler":
        result["world_sampler_sha"] = "a" * 64
    elif case == "actual-mutated":
        first["actual_digest_after"] = "a" * 64
    elif case == "lease":
        first["lease_released"] = False
    elif case == "phase-restore":
        first["world_lifecycle"]["phase_restores"] -= 1
    elif case == "boundary-chain":
        first["attempts"][0]["transitions"][0]["before_tick"] += 1
    elif case == "incomplete-boundary":
        first["attempts"][0]["transitions"][0]["completed"] = False
    elif case == "true-hidden-not-changed":
        record = native["records"][1]
        record["mutation"]["after_digest"] = record["mutation"]["before_digest"]
    elif case == "copy-actual-state":
        manifest = first["audit"]["world_manifest"]
        manifest["actual_state_copied"] = True
        manifest["conditioning"]["actual_state_copied"] = True
        from native_runner.training.fair_search.contracts import semantic_digest
        result["world_commitment"] = semantic_digest(manifest)
    elif case == "admission":
        native["training_admissible"] = True
    elif case == "scope":
        native["exclusions"].remove("general-pending-plan")
    elif case == "cleanup":
        cleanup = documents[PREFIX + "cleanup.json"]
        cleanup["engines"][0]["listening"] = True
        documents["run-results.json"] = [deepcopy(cleanup)]
    elif case == "resource-gate":
        documents["preflight.json"]["formal_storage_gate_passed"] = True
    elif case == "attempt-failed":
        documents["attempt.json"]["exit_code"] = 1
    elif case == "engine-identity":
        documents["attempt.json"]["engine_sha256"] = "a" * 64
    elif case == "duration":
        native["seconds"] = float("nan")
    else:
        raise AssertionError(case)
    documents[PREFIX + "progress.json"] = dict(records=native["records"], replayed=native["replayed"])


@pytest.mark.parametrize("case", ["budget", "unfinished", "visits", "noise", "viewpoint", "sampler",
    "actual-mutated", "lease", "phase-restore", "boundary-chain", "incomplete-boundary",
    "true-hidden-not-changed", "copy-actual-state", "admission", "scope", "cleanup", "resource-gate", "attempt-failed",
    "engine-identity", "duration"])
def test_tampered_native_evidence_is_rejected(case, monkeypatch, attempt_directory):
    names = ["attempt.json", "preflight.json", "run-results.json", PREFIX + "report.json",
        PREFIX + "progress.json", PREFIX + "cleanup.json"]
    documents = {name: json.loads((attempt_directory / name).read_text()) for name in names}
    mutate_case(case, documents)
    overrides = {attempt_directory / name: json.dumps(value) for name, value in documents.items()}
    original = Path.read_text

    def intercepted(path, *args, **kwargs):
        return overrides[path.resolve()] if path.resolve() in overrides else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", intercepted)
    with pytest.raises(ValueError):
        validate(attempt_directory)
