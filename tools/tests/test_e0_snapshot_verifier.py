import copy
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e0_snapshots import summarize_counts, verify


def fixture(tmp_path):
    directories = [tmp_path / "baseline", tmp_path / "observed"]
    for directory in directories:
        directory.mkdir()
        for tick in range(3):
            (directory / f"tick-{tick:06d}.bin").write_bytes(bytes([1, tick, 255]))
    receipt = dict(ok=True, error_code=0, finalized=True, queue_count=0, start_tick=0,
                   stagnant=0, tick=2, snapshots=3, injected=2, world_result_raw=1)
    return directories, receipt


def test_complete_bytes_compare_without_normalization(tmp_path):
    directories, receipt = fixture(tmp_path)
    result = verify(*directories, receipt, receipt)
    assert result["snapshot_count"] == 3
    assert result["total_bytes"] == 9
    assert not result["training_admissible"]
    assert not result["vm_framework_free_verified"]


@pytest.mark.parametrize("field,value", [("finalized", False), ("queue_count", 1),
    ("error_code", 3), ("stagnant", 1), ("start_tick", 1), ("tick", False)])
def test_reject_incomplete_or_invalid_receipts(tmp_path, field, value):
    directories, receipt = fixture(tmp_path)
    bad = dict(receipt, **{field: value})
    with pytest.raises(ValueError):
        verify(*directories, receipt, bad)


@pytest.mark.parametrize("mutation", ["byte", "missing", "extra", "empty"])
def test_reject_snapshot_mutations(tmp_path, mutation):
    directories, receipt = fixture(tmp_path)
    target = directories[1] / "tick-000001.bin"
    if mutation == "missing":
        target.unlink()
    elif mutation == "extra":
        (directories[1] / "extra.bin").write_bytes(b"unexpected")
    else:
        target.write_bytes(b"changed" if mutation == "byte" else b"")
    with pytest.raises(ValueError):
        verify(*directories, receipt, receipt)


def test_failed_hooks_stay_unknown_and_first_phase_uses_sequence():
    evidence = dict(schema="e0-dependency-counts.v1", engine_loaded=True,
      coverage=[dict(label="missing", status="failed"), dict(label="silent", status="attached")],
      counts=[dict(labels=["JNI"], count=3, phase="tick", first_sequence=10),
              dict(labels=["JNI"], count=2, phase="preinit", first_sequence=1)],
      errors=[{"error": "test failure"}], scope="configured hooks")
    original = copy.deepcopy(evidence)
    summary = summarize_counts(evidence)
    assert summary["labels"]["missing"]["count"] is None
    assert summary["labels"]["silent"]["count"] == 0
    assert summary["labels"]["JNI"]["count"] == 5
    assert summary["labels"]["JNI"]["first_phase"] == "preinit"
    assert not summary["exhaustive"]
    assert original == evidence


@pytest.mark.parametrize("field", ["world_result_raw", "snapshots", "injected"])
def test_reject_missing_native_fields_even_when_both_receipts_match(tmp_path, field):
    directories, receipt = fixture(tmp_path)
    del receipt[field]
    with pytest.raises(ValueError):
        verify(*directories, receipt, receipt)


def test_shared_targets_are_explicitly_nonadditive():
    summary = summarize_counts(dict(schema="e0-dependency-counts.v1", engine_loaded=True,
        coverage=[], counts=[dict(labels=["provider_a", "provider_b"], count=2,
        phase="resources", first_sequence=0)], errors=[], scope="configured hooks"))
    assert summary["labels"]["provider_a"]["count"] == 2
    assert summary["labels"]["provider_b"]["count"] == 2
    assert summary["label_counts_additive"] is False


@pytest.mark.parametrize("bad_count", [0, -1, 1.5, True])
def test_reject_fabricated_observation_counts(bad_count):
    with pytest.raises(ValueError):
        summarize_counts(dict(schema="e0-dependency-counts.v1", engine_loaded=True,
          coverage=[], counts=[dict(labels=["JNI"], count=bad_count)], errors=[], scope="test"))
