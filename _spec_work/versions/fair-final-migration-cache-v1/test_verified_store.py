import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-series-v1"),
    str(ROOT.parents[2] / "firstlight_CR")]

from test_series_training import series
from verified_store import VerifiedEpisodeStore
from native_runner.training.fair_migration_data import MigrationStore
from native_runner.training.fair_search.contracts import semantic_digest


def store_for(series, tmp_path):
    source = series["initial"].stores[0]
    destination = tmp_path / "data"
    shutil.copytree(source.root, destination)
    return VerifiedEpisodeStore(destination, manifest=source.manifest)


def test_cache_preserves_full_content_and_protects_against_caller_mutation(series, monkeypatch, tmp_path):
    store = store_for(series, tmp_path)
    match = next(iter(store.manifest["matches"]))
    first, receipt = store.load(match)
    identity = semantic_digest(first)
    first["rows"][0]["teacher_wdl"].zero_()
    first["rows"].pop()
    monkeypatch.setattr(MigrationStore, "_read", lambda *args: pytest.fail("cache miss"))
    second, second_receipt = store.load(match)
    assert identity == semantic_digest(second)
    assert receipt == second_receipt


@pytest.mark.parametrize("change", ["file", "receipt", "quarantine", "manifest"])
def test_cache_hit_still_rejects_changed_evidence(series, change, tmp_path):
    store = store_for(series, tmp_path)
    match = next(iter(store.manifest["matches"]))
    _, receipt = store.load(match)
    if change == "file":
        path = store.root / "episodes" / (receipt["semantic_sha"] + ".pt.gz")
        path.write_bytes(path.read_bytes() + b"changed")
    elif change == "receipt":
        path = store.root / "completed" / (match + ".json")
        payload = json.loads(path.read_text())
        payload["rows"] += 1
        path.write_text(json.dumps(payload))
    elif change == "quarantine":
        path = store.root / "attempts" / match / str(receipt["attempt_id"]) / "quarantine.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    else:
        store.manifest["namespace_sha"] = "0" * 64
    with pytest.raises(ValueError):
        store.load(match)


def test_cache_retains_only_last_episode_and_revalidates_evicted_entry(series, monkeypatch, tmp_path):
    store = store_for(series, tmp_path)
    matches = list(store.manifest["matches"])
    calls = []
    original = MigrationStore._read

    def read(instance, digest, expected_sha):
        calls.append(digest)
        return original(instance, digest, expected_sha)

    monkeypatch.setattr(MigrationStore, "_read", read)
    store.load(matches[0])
    store.load(matches[0])
    store.load(matches[1])
    store.load(matches[0])
    assert len(calls) == 3


def test_selected_rows_preserve_order_and_do_not_copy_whole_episode(series, monkeypatch, tmp_path):
    import verified_store
    store = store_for(series, tmp_path)
    match = next(iter(store.manifest["matches"]))
    episode, receipt = store.load(match)
    original = verified_store.deepcopy

    def copy_rows(value):
        assert isinstance(value, list) and len(value) == 2
        return original(value)

    monkeypatch.setattr(verified_store, "deepcopy", copy_rows)
    rows, actual = store.load_rows(match, [1, 0])
    assert semantic_digest(rows) == semantic_digest([episode["rows"][1], episode["rows"][0]])
    assert actual == receipt
    rows[0]["teacher_wdl"].zero_()
    repeated, _ = store.load_rows(match, [1, 0])
    assert semantic_digest(repeated) == semantic_digest([episode["rows"][1], episode["rows"][0]])
    for offsets in ([], [0, 0], [True], [-1], [len(episode["rows"])]):
        with pytest.raises(ValueError):
            store.load_rows(match, offsets)
