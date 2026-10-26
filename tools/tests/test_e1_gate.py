import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e1_gate import evaluate, RESOURCE_IMPORTS, SEMANTIC_CHECKS


def evidence():
    return [dict(schema="e0-snapshot-comparison.v1", status="passed", snapshot_count=3),
        dict(schema="e0-dependency-counts.v1", engine_loaded=True, counts=[],
             coverage=[dict(label=name, status="attached") for name in RESOURCE_IMPORTS],
             errors=[], scope="synthetic engineering fixture"),
        dict(receipt=dict(installed=True, errors=[], redirects=[dict(name=name, verified=True)
             for name in RESOURCE_IMPORTS]), metrics=dict(failures="0", live_assets="0")),
        dict.fromkeys(SEMANTIC_CHECKS, True)]


def test_fixture_gate_does_not_claim_vm_or_training():
    result = evaluate(*evidence())
    assert result["status"] == "passed"
    assert not result["vm_framework_free_verified"]
    assert not result["training_admissible"]
    assert not result["tenfold_speedup_verified"]


@pytest.mark.parametrize("field", SEMANTIC_CHECKS)
def test_missing_semantic_evidence_blocks(field):
    inputs = evidence()
    inputs[3].pop(field)
    assert evaluate(*inputs)["status"] == "blocked"


def test_fmod_or_any_caller_remaining_is_not_filtered_and_aliases_not_added():
    inputs = evidence()
    inputs[1]["counts"] = [dict(labels=["AAssetManager_fromJava", "AAssetAlias"],
        count=1, phase="load", first_sequence=0, caller="synthetic_other_library")]
    result = evaluate(*inputs)
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
    assert evaluate(*inputs)["status"] == "blocked"
    assert inputs == original
