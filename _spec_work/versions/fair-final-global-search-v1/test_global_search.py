from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from snapshot_audit import SCOPE, SnapshotLedger, checked_digest, validate_records
import run_search
import verify_search


def digest():
    return dict(scope=SCOPE, tick=110, sha256="a" * 64, nativeSha256="b" * 64)


def created(handle=1):
    return dict(ok=True, handle=handle, scope="process", tick=110,
        generation=2, configRevision=3, stateEpoch=5)


def restored(handle=1, epoch=6):
    return dict(ok=True, restored=True, handle=handle, tick=110,
        generation=2, configRevision=3, stateEpoch=epoch)


def released(handle=1):
    return dict(ok=True, released=True, handle=handle, generation=2)


def complete_ledger(restores=2):
    ledger = SnapshotLedger()
    ledger.create(created(), digest())
    for ordinal in range(restores):
        ledger.restore(1, restored(epoch=6 + ordinal), digest())
    ledger.release(1, released())
    return ledger


def test_repeated_restore_and_independent_record_recomputation():
    ledger = complete_ledger()
    assert validate_records(ledger.records) == dict(
        counts=dict(create=1, restore=2, release=1), outstanding=0, scope=SCOPE)


@pytest.mark.parametrize("field,value", [("scope", "native-payload.v1"),
    ("tick", True), ("tick", -1), ("sha256", "A" * 64), ("nativeSha256", "b" * 63)])
def test_digest_rejects_old_scope_and_ambiguous_encoding(field, value):
    changed = digest()
    changed[field] = value
    with pytest.raises(ValueError, match="scope or encoding"):
        checked_digest(changed)


@pytest.mark.parametrize("field,value", [("generation", True), ("configRevision", 0),
    ("stateEpoch", False), ("handle", True), ("ok", 1), ("tick", 112)])
def test_snapshot_creation_rejects_identity_changes(field, value):
    receipt = created()
    receipt[field] = value
    with pytest.raises(ValueError, match="creation identity"):
        SnapshotLedger().create(receipt, digest())


@pytest.mark.parametrize("field", ["sha256", "nativeSha256", "tick"])
def test_restore_detects_global_only_native_only_and_tick_divergence(field):
    ledger = SnapshotLedger()
    ledger.create(created(), digest())
    changed = digest()
    changed[field] = 112 if field == "tick" else "c" * 64
    with pytest.raises(ValueError, match="complete digest"):
        ledger.restore(1, restored(), changed)
    assert len(ledger.records) == 1


@pytest.mark.parametrize("field,value", [("generation", 4), ("configRevision", 4),
    ("stateEpoch", 5), ("stateEpoch", True), ("restored", 1), ("handle", True)])
def test_restore_rejects_stale_or_wrong_owner_receipt(field, value):
    ledger = SnapshotLedger()
    ledger.create(created(), digest())
    receipt = restored()
    receipt[field] = value
    with pytest.raises(ValueError, match="restoration identity"):
        ledger.restore(1, receipt, digest())


def test_duplicate_reuse_release_and_outstanding_handles_fail():
    ledger = SnapshotLedger()
    ledger.create(created(), digest())
    with pytest.raises(ValueError):
        ledger.create(created(), digest())
    with pytest.raises(ValueError, match="remain active"):
        ledger.summary()
    with pytest.raises(ValueError):
        ledger.release(True, released())
    ledger.release(1, released())
    with pytest.raises(ValueError):
        ledger.restore(1, restored(), digest())
    with pytest.raises(ValueError):
        ledger.release(1, released())


@pytest.mark.parametrize("change", ["omit_release", "extra_field", "wrong_operation", "changed_restore"])
def test_independent_records_reject_incomplete_or_mutated_audit(change):
    records = deepcopy(complete_ledger().records)
    if change == "omit_release":
        records.pop()
    elif change == "extra_field":
        records[0]["ignored"] = True
    elif change == "wrong_operation":
        records[0]["operation"] = "completed"
    else:
        records[1]["digest"]["sha256"] = "c" * 64
    with pytest.raises(ValueError):
        validate_records(records)


def test_actual_and_belief_lifecycle_coverage():
    ledger = complete_ledger(restores=256)
    audits = [dict(port=port, records=deepcopy(ledger.records), summary=ledger.summary())
        for port in run_search.PORTS]
    assert verify_search.validate_audits(audits) == dict(create=2, restore=512, release=2)
    audits[1]["port"] = audits[0]["port"]
    with pytest.raises(ValueError, match="coverage absent"):
        verify_search.validate_audits(audits)


def test_summary_is_recomputed_instead_of_trusted():
    ledger = complete_ledger(restores=256)
    audits = [dict(port=port, records=deepcopy(ledger.records), summary=ledger.summary())
        for port in run_search.PORTS]
    audits[0]["summary"]["counts"]["restore"] += 1
    with pytest.raises(ValueError, match="summary differs"):
        verify_search.validate_audits(audits)


@pytest.fixture
def per_root_audit():
    ledger = complete_ledger(restores=512)
    records = [dict(tick=110, fair_sha="f" * 64,
        world_lifecycle=dict(root_digest="a" * 64, restores=128)) for _ in range(4)]
    audits = [dict(port=run_search.PORTS[1], records=ledger.records)]
    searches = [dict(tick=110, fair_sha="f" * 64,
        spans=[dict(transport=0, start=1 + index * 128, end=1 + (index + 1) * 128)])
        for index in range(4)]
    return records, audits, searches


def test_each_root_is_linked_to_its_own_complete_digest_restores(per_root_audit):
    verify_search.validate_spans(*per_root_audit)


@pytest.mark.parametrize("change", ["missing", "overlap", "short", "wrong_root", "wrong_port", "bool_index"])
def test_total_restore_count_never_substitutes_per_root_evidence(per_root_audit, change):
    records, audits, searches = per_root_audit
    if change == "missing":
        searches.pop()
    elif change == "overlap":
        searches[1]["spans"] = searches[0]["spans"]
    elif change == "short":
        searches[0]["spans"][0]["end"] -= 1
    elif change == "wrong_root":
        records[0]["world_lifecycle"]["root_digest"] = "c" * 64
    elif change == "wrong_port":
        audits[0]["port"] = run_search.PORTS[0]
    else:
        searches[0]["spans"][0]["transport"] = False
    with pytest.raises(ValueError):
        verify_search.validate_spans(records, audits, searches)


def test_audited_transport_reads_every_restored_digest(monkeypatch):
    from native_runner.training.fair_search.transport import CompactSearchNative
    from probe_search import AuditedNative
    commands = []
    state = dict(value=digest())
    def control(self, command, **unused):
        commands.append(command)
        return deepcopy(state["value"] if command == "state-digest" else
            created() if command == "snapshot-create" else
            released() if command.startswith("release-snapshot") else
            restored(epoch=6 + commands.count("restore 1")))
    monkeypatch.setattr(CompactSearchNative, "_request", control)
    native = object.__new__(AuditedNative)
    native.snapshot_ledger = SnapshotLedger()
    native._request("snapshot-create")
    native._request("restore 1")
    assert commands == ["state-digest", "snapshot-create", "restore 1", "state-digest"]
    state["value"]["sha256"] = "c" * 64
    with pytest.raises(ValueError, match="complete digest"):
        native._request("restore 1")
    native._request("release-snapshot 1")
    assert native.snapshot_ledger.summary()["counts"]["restore"] == 1


@pytest.mark.skipif(not (run_search.BUILD / "libcrprobe.so").exists(), reason="private native build absent")
def test_real_payload_uses_repaired_engine_and_exact_frozen_sources():
    files, sources = run_search.payloads("/independent-test")
    assert hashlib.sha256(files["libcrprobe.so"]).hexdigest() == "fbd97f8f02621d37adc41b9782440950d9e0346ad2cffcb68601c01fb237141e"
    assert json.loads(files["project/probe-source-manifest.json"]) == sources
    for name, expected in sources.items():
        assert hashlib.sha256(files["project/" + name]).hexdigest() == expected
    assert set(run_search.PORTS).isdisjoint({29761, 29762})
    assert files["project/probe_search.py"] == (Path(__file__).parent / "probe_search.py").read_bytes()


@pytest.mark.skipif(not (run_search.PARENT / "download.json").exists(), reason="private parent evidence absent")
def test_real_parent_is_independently_revalidated():
    result = run_search.validate_parent(run_search.PARENT, run_search.PARENT_LAUNCH, run_search.PARENT_RECEIPT)
    assert result["status"] == "verified_limited_scope"
    assert result["training_admissible"] is False
    assert result["gates_closed"] is False
