"""Local migration integration tests; no engine or remote services."""
from dataclasses import replace
import json

import pytest
import zstandard

from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.event_cache import (
    EventObservationRecordV1, default_event_contract, load_event_cache_shard,
    validate_event_supervised_contract, write_event_cache_shard,
)
from native_runner.training.v4.cache import _CONTAINER_HEADER, _canonical_json, _sha256
from tools.migrate_event_cache_v2 import migrate, replay_partition, main


def record(tag, owner=0):
    return EventObservationRecordV1(
        replay_tag=tag, owner=owner, observation_tick=0, trigger_event_id="start",
        trigger_owner=None, trigger_card_id=None, wait=False, card_id=1,
        hand_slot=0, target_cell=(3, 4), delay_ms=100, delay_bin=1,
        target_tick=1, next_trigger_tick=None, terminal=False, candidate_index=0,
        candidate_uid=42, legal_at_trigger=True, card_in_hand=True,
    )


def shard(root, name, rows, contract=None):
    root.mkdir(parents=True, exist_ok=True)
    write_event_cache_shard(root / f"{name}.crev.zst", rows, [_batch(1) for _ in rows],
                            shard_id=name, contract=contract or {"version": "event-observation-cache-contract.v1"})


def tensor_bytes(path):
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
    _, length = _CONTAINER_HEADER.unpack_from(raw)
    return raw[_CONTAINER_HEADER.size + length:]


def run(tmp_path, mode="native", **kwargs):
    evidence = tmp_path / "proof.json"
    if not evidence.exists():
        evidence.write_text('{"external_audit": "fixture"}')
    return migrate(tmp_path / "old", tmp_path / "new", confirmed_cell_frame=mode,
                   evidence_file=evidence, **kwargs)


@pytest.mark.parametrize("mode,workers", [("native", 1), ("model", 2)])
def test_roundtrip_split_storage_resume(tmp_path, mode, workers):
    tags = {p: next(f"replay-{i}" for i in range(1000) if replay_partition(f"replay-{i}") == p)
            for p in ("train", "val")}
    for partition, tag in tags.items():
        rows = [record(tag, 0), record(tag, 1), replace(record(tag, 1), wait=True,
                card_id=None, target_cell=None, legal_at_trigger=False)]
        shard(tmp_path / "old" / "nested", partition, rows)
    original = {str(p.relative_to(tmp_path / "old")): p.read_bytes()
                for p in (tmp_path / "old").rglob("*") if p.is_file()}
    indexes = run(tmp_path, mode, workers=workers)
    seen = {}
    for partition, index in indexes.items():
        assert index["schema"] == "event-observation-cache-index.v1"
        unsigned = dict(index)
        digest = unsigned.pop("index_sha256")
        assert digest == _sha256(_canonical_json(unsigned))
        validate_event_supervised_contract(index["contract"])
        assert index["frames"] == 3
        entry = index["shards"][0]
        root = tmp_path / "new" / partition
        loaded = load_event_cache_shard(root / entry["summary_file"], verify=True)
        validate_event_supervised_contract(loaded.manifest["contract"])
        assert loaded.records[0].target_cell == (3, 4)
        assert loaded.records[1].target_cell == ((14, 27) if mode == "native" else (3, 4))
        assert loaded.records[2].target_cell is None
        assert tensor_bytes(root / entry["data_file"]) == tensor_bytes(tmp_path / "old" / entry["data_file"])
        assert loaded.manifest["migration"]["evidence_sha256"] == _sha256((tmp_path / "proof.json").read_bytes())
        seen[partition] = {r.replay_tag for r in loaded.records}
    assert seen["train"].isdisjoint(seen["val"])
    assert original == {str(p.relative_to(tmp_path / "old")): p.read_bytes()
                        for p in (tmp_path / "old").rglob("*") if p.is_file()}
    output_before = {str(p): p.read_bytes() for p in (tmp_path / "new").rglob("*") if p.is_file()}
    assert run(tmp_path, mode, workers=workers) == indexes
    assert output_before == {str(p): p.read_bytes() for p in (tmp_path / "new").rglob("*") if p.is_file()}


def test_reject_mixed_partition(tmp_path):
    tags = [next(str(i) for i in range(1000) if replay_partition(str(i)) == p) for p in ("train", "val")]
    shard(tmp_path / "old", "mixed", [record(t) for t in tags])
    with pytest.raises(ValueError, match="mixed train/val"):
        run(tmp_path)
    assert not list((tmp_path / "new").rglob("*.crev.zst"))


def test_reject_v2_double_migration(tmp_path):
    shard(tmp_path / "old", "v2", [record("a", 1)], default_event_contract())
    with pytest.raises(ValueError, match="already-v2"):
        run(tmp_path, "model")


@pytest.mark.parametrize("tamper", ["source", "output", "summary", "evidence"])
def test_resume_rejects_changes(tmp_path, tamper):
    shard(tmp_path / "old", "one", [record("a")])
    run(tmp_path)
    partition = replay_partition("a")
    paths = {"source": tmp_path / "old" / "one.crev.zst",
             "output": tmp_path / "new" / partition / "one.crev.zst",
             "summary": tmp_path / "new" / partition / "one.json",
             "evidence": tmp_path / "proof.json"}
    with paths[tamper].open("ab") as f:
        f.write(b" ")
    with pytest.raises(ValueError, match="checksum|differs"):
        run(tmp_path)


def test_duplicates_are_preserved_and_unjournaled_outputs_resume(tmp_path):
    shard(tmp_path / "old" / "first", "one", [record("a")])
    import shutil
    shutil.copytree(tmp_path / "old" / "first", tmp_path / "old" / "duplicate")
    indexes = run(tmp_path)
    p = replay_partition("a")
    assert indexes[p]["shard_count"] == 2
    (tmp_path / "new" / p / "migration.jsonl").write_bytes(b"")
    assert run(tmp_path) == indexes


@pytest.mark.parametrize("mode,frame", [("native", "actor_relative.v1"), ("model", "native.v1")])
def test_reject_contradictory_contract(tmp_path, mode, frame):
    shard(tmp_path / "old", "one", [record("a")],
          {"version": "event-observation-cache-contract.v1", "action_cell_frame": frame})
    with pytest.raises(ValueError, match="contradicts"):
        run(tmp_path, mode)


def test_verify_uncompressed_checksum(tmp_path):
    shard(tmp_path / "old", "one", [record("a")])
    summary_path = tmp_path / "old" / "one.json"
    summary = json.loads(summary_path.read_text())
    summary["container_sha256"] = "0" * 64
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="container checksum"):
        run(tmp_path)


def test_missing_summary_fails(tmp_path):
    shard(tmp_path / "old", "one", [record("a")])
    (tmp_path / "old" / "one.json").unlink()
    with pytest.raises(FileNotFoundError):
        run(tmp_path)


def test_cli_requires_frame_and_evidence():
    with pytest.raises(SystemExit) as exc:
        main(["--source", "old", "--output", "new"])
    assert exc.value.code == 2


def test_no_nested_output(tmp_path):
    shard(tmp_path / "old", "one", [record("a")])
    evidence = tmp_path / "proof"
    evidence.write_text("external proof")
    with pytest.raises(ValueError, match="non-nested"):
        migrate(tmp_path / "old", tmp_path / "old" / "new", confirmed_cell_frame="native", evidence_file=evidence)
