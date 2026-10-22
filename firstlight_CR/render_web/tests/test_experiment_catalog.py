import json
import errno
import hashlib

import pytest

from render_web import remote_agent
from render_web import checkpoint_sources as sources
from render_web.config import write_json
from render_web.tests.test_workbench import api, checkpoint


@pytest.fixture
def trial(tmp_path):
    root = tmp_path / "event-mixed-deck-100r-20260920/tower-beta1-formal1553-200r-20260922"
    root.mkdir(parents=True)
    reward = dict(version="elixir-overflow-tower.v4")
    write_json(root / "protocol.json", dict(run_id=root.name, start_iteration=1553,
               start_sha256="a" * 64, updates=200, reward_config=reward))
    first = root / "tower/round-001"
    first.mkdir(parents=True)
    write_json(first / "success.json", dict(run_id=root.name, round=1, iteration=1554,
               checkpoint_sha256="b" * 64, parent_checkpoint_sha256="a" * 64,
               reward_config=reward))
    write_json(first / "update-verified.json", dict(checkpoint_sha256="b" * 64,
               preflight=dict(passed=True), verifier=dict(iterations=1554)))
    # A checkpoint saved before audit/success must never appear in the catalog.
    pending = root / "tower/round-002/updates/attempt"
    pending.mkdir(parents=True)
    (pending / "updated.pt").write_bytes(b"unfinished checkpoint")
    return root


def test_trial_scan_excludes_incomplete_update_and_keeps_archive_location(tmp_path, trial):
    rows = remote_agent.experiment_entries(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["run"] == str(trial.relative_to(tmp_path))
    assert row["category"] == "tower-beta1-formal1553"
    assert row["iteration"] == 1554 and row["trial_round"] == 1
    assert row["trial_updates"] == 200


@pytest.mark.parametrize("field,value", [
    ("parent_checkpoint_sha256", "c" * 64), ("run_id", "another-experiment"),
    ("iteration", 1555), ("reward_config", {"version": "other"}),
])
def test_trial_scan_rejects_foreign_or_broken_lineage(tmp_path, trial, field, value):
    path = trial / "tower/round-001/success.json"
    success = json.loads(path.read_text())
    success[field] = value
    write_json(path, success)
    with pytest.raises(ValueError, match="身份校验"):
        remote_agent.experiment_entries(tmp_path)


def test_trial_scan_requires_readback_pass(tmp_path, trial):
    path = trial / "tower/round-001/update-verified.json"
    verified = json.loads(path.read_text())
    verified["preflight"]["passed"] = False
    write_json(path, verified)
    with pytest.raises(ValueError, match="身份校验"):
        remote_agent.experiment_entries(tmp_path)


def test_catalog_latest_separates_formal_and_trial(api):
    client, store, _ = api
    formal = dict(checkpoint("formal"), iteration=1553, category="formal", category_label="正式训练")
    trial = dict(checkpoint("trial"), iteration=1717, category="tower-beta1-formal1553",
                 category_label="塔血实验 β=1 · 1553 起点")
    settlement = dict(checkpoint("settlement"), iteration=1561,
                      category=sources.SETTLEMENT_CATEGORY, category_label=sources.SETTLEMENT_LABEL)
    unfinished = dict(trial, id="unavailable", iteration=1718, available=False)
    store.replace_checkpoints([formal, trial, unfinished, settlement])
    data = client.get("/api/checkpoints").json()
    assert data["latest"] == trial["id"]
    assert {c["id"]:c["iteration"] for c in data["categories"]} == {
        "formal":1553, "tower-beta1-formal1553":1717, sources.SETTLEMENT_CATEGORY:1561}


@pytest.fixture
def settlement_trial(tmp_path):
    root = tmp_path / sources.SETTLEMENT_RUN / "training"
    root.mkdir(parents=True)
    reward = dict(version="elixir-settlement-nearquiet.v1", penalty_per_elixir=.005,
                  penalty_cap=None, settlement=dict(version="settlement-nearquiet.v1", beta=1/3))
    write_json(root / "protocol.json", dict(run_id=sources.SETTLEMENT_RUN, start_iteration=1553,
               start_sha256=sources.FORMAL1553_SHA, updates=200, reward_config=reward,
               gamma=.999, gae_lambda=.95))
    write_json(root / "experiment-config.json", dict(mode="training", run_id=sources.SETTLEMENT_RUN))
    write_json(root / "first-round-acceptance.json", dict(passed=True, start_iteration=1553,
               parent_checkpoint_sha256=sources.FORMAL1553_SHA, checkpoint_sha256="b"*64,
               optimizer_resumed=True, settlement_same_trace=True))
    for number, sha, parent in ((1, "b"*64, sources.FORMAL1553_SHA), (2, "c"*64, "b"*64)):
        directory = root / "tower" / f"round-{number:03d}"
        directory.mkdir(parents=True)
        write_json(directory / "success.json", dict(run_id=sources.SETTLEMENT_RUN, round=number,
                   iteration=1553+number, checkpoint_sha256=sha, parent_checkpoint_sha256=parent,
                   reward_config=reward))
        write_json(directory / "update-verified.json", dict(checkpoint_sha256=sha,
                   preflight=dict(passed=True, reward_config=reward), advanced_parameters=359,
                   optimizer_resumed=dict(resumed=True, iterations=1552+number),
                   verifier=dict(checkpoint_sha256=sha, iterations=1553+number, optimizer_steps=1553+number)))
    pending = root / "tower/round-003/updates/attempt"
    pending.mkdir(parents=True)
    (pending / "updated.pt").write_bytes(b"unfinished")
    return root


def test_settlement_only_exposes_accepted_complete_training_updates(settlement_trial):
    rows = remote_agent.settlement_entries(settlement_trial)
    assert [x["iteration"] for x in rows] == [1554, 1555]
    assert all(x["run"] == sources.SETTLEMENT_RUN and x["category"] == sources.SETTLEMENT_CATEGORY
               and x["label_prefix"] == "近静场结算 β=1/3" for x in rows)
    (settlement_trial / "first-round-acceptance.json").unlink()
    assert remote_agent.settlement_entries(settlement_trial) == []


@pytest.mark.parametrize("filename,field,value", [
    ("protocol.json", "start_sha256", "a"*64),
    ("protocol.json", "gae_lambda", .99),
    ("experiment-config.json", "mode", "benchmark"),
    ("first-round-acceptance.json", "passed", False),
    ("first-round-acceptance.json", "checkpoint_sha256", "d"*64),
    ("tower/round-002/success.json", "parent_checkpoint_sha256", "d"*64),
    ("tower/round-002/success.json", "reward_config", {"version": "elixir-overflow-tower.v4"}),
    ("tower/round-002/update-verified.json", "optimizer_resumed", {"resumed": False, "iterations":1554}),
    ("tower/round-002/update-verified.json", "verifier", {"checkpoint_sha256":"d"*64, "iterations":1555, "optimizer_steps":1555}),
])
def test_settlement_rejects_other_branch_or_unverified_update(settlement_trial, filename, field, value):
    path = settlement_trial / filename
    data = json.loads(path.read_text())
    data[field] = value
    write_json(path, data)
    with pytest.raises(ValueError):
        remote_agent.settlement_entries(settlement_trial)


def test_settlement_weight_path_is_pinned_and_video_can_cross_filesystems(tmp_path, monkeypatch):
    root = tmp_path / "data/training"
    monkeypatch.setattr(sources, "SETTLEMENT_ROOT", str(root))
    payload = b"complete settlement checkpoint"
    sha = hashlib.sha256(payload).hexdigest()
    entry = dict(run=sources.SETTLEMENT_RUN, category=sources.SETTLEMENT_CATEGORY, sha256=sha)
    path, allowed = sources.checkpoint_location(tmp_path / "archive", entry)
    assert str(allowed) == str(root)
    root.joinpath("weights").mkdir(parents=True)
    root.joinpath("weights", sha + ".pt").write_bytes(payload)
    entry["path"] = str(path)
    destination = tmp_path / "video.pt"
    def cross_device(*args):
        raise OSError(errno.EXDEV, "different filesystem")
    monkeypatch.setattr(remote_agent.os, "link", cross_device)
    remote_agent.stage_checkpoint({"archive_root":str(tmp_path / "archive")}, entry, destination)
    assert destination.read_bytes() == payload
    assert not destination.with_suffix(".copying").exists()
    # Existing files are verified again before reuse.
    destination.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="SHA mismatch"):
        remote_agent.stage_checkpoint({"archive_root":str(tmp_path / "archive")}, entry, destination)
    for patch in ({"run":"../other"}, {"run":"/data/other"}, {"run":"other-run"}, {"category":"formal"}):
        with pytest.raises(ValueError):
            sources.checkpoint_location(tmp_path / "archive", {**entry, **patch})
    with pytest.raises(ValueError, match="path"):
        remote_agent.stage_checkpoint({"archive_root":str(tmp_path / "archive")},
                                      {**entry, "path":"/etc/passwd"}, destination)


@pytest.fixture
def continuation_trial(tmp_path):
    root = tmp_path / sources.CONTINUATION_RUN
    root.mkdir()
    reward = dict(version="elixir-overflow-tower.v6", penalty_per_elixir=.003,
                  penalty_cap=None, tower_potential=dict(version="visible-tower-hp.v1",
                                                         coefficient=.15, gamma=.999, terminal="zero"))
    previous_reward = dict(version="elixir-overflow-tower.v4",
                           tower_potential=dict(coefficient=1.0))
    write_json(root / "protocol.json", dict(run_id=sources.CONTINUATION_RUN,
               start_iteration=1753, start_sha256=sources.CONTINUATION_START_SHA,
               source_run="tower-beta1-formal1553-200r-20260922",
               checkpoint=str(root / "weights" / (sources.CONTINUATION_START_SHA + ".pt")),
               resume_optimizer=True, reward_config=reward,
               reward_switch=dict(iteration=1754, from_config=previous_reward, to_config=reward),
               ppo=dict(gamma=.999, gae_lambda=.95)))
    write_json(root / "first-round-acceptance.json", dict(passed=True, games=108,
               start_iteration=1753, iteration=1754,
               parent_checkpoint_sha256=sources.CONTINUATION_START_SHA,
               checkpoint_sha256="b" * 64, reward_config=reward,
               optimizer_resumed=True, advanced_parameters=359))
    for number, sha, parent in ((1, "b" * 64, sources.CONTINUATION_START_SHA),
                                (2, "c" * 64, "b" * 64)):
        directory = root / "tower" / f"round-{number:03d}"
        directory.mkdir(parents=True)
        write_json(directory / "success.json", dict(run_id=sources.CONTINUATION_RUN,
                   round=number, iteration=1753 + number,
                   checkpoint_sha256=sha, parent_checkpoint_sha256=parent,
                   reward_config=reward))
        write_json(directory / "update-verified.json", dict(checkpoint_sha256=sha,
                   preflight=dict(passed=True, reward_config=reward),
                   advanced_parameters=359,
                   optimizer_resumed=dict(resumed=True, iterations=1752 + number),
                   verifier=dict(checkpoint_sha256=sha, iterations=1753 + number,
                                 optimizer_steps=1753 + number)))
    pending = root / "tower/round-003/updates/attempt"
    pending.mkdir(parents=True)
    (pending / "updated.pt").write_bytes(b"unfinished")
    return root


def test_continuation_indexes_only_complete_updates(continuation_trial):
    rows = remote_agent.continuation_entries(continuation_trial)
    assert [row["iteration"] for row in rows] == [1754, 1755]
    assert [row["trial_round"] for row in rows] == [1, 2]
    assert all(row["trial_updates"] is None and row["category"] == sources.CONTINUATION_CATEGORY
               and row["run"] == sources.CONTINUATION_RUN for row in rows)
    (continuation_trial / "first-round-acceptance.json").unlink()
    assert remote_agent.continuation_entries(continuation_trial) == []


@pytest.mark.parametrize("filename,field,value", [
    ("protocol.json", "start_sha256", "a" * 64),
    ("protocol.json", "source_run", "other-run"),
    ("protocol.json", "checkpoint", "/tmp/other.pt"),
    ("protocol.json", "reward_config", {"version": "elixir-overflow-tower.v4"}),
    ("first-round-acceptance.json", "checkpoint_sha256", "d" * 64),
    ("tower/round-002/success.json", "parent_checkpoint_sha256", "d" * 64),
    ("tower/round-002/update-verified.json", "optimizer_resumed", {"resumed": False, "iterations": 1754}),
    ("tower/round-002/update-verified.json", "verifier", {"checkpoint_sha256": "d" * 64,
                                                       "iterations": 1755, "optimizer_steps": 1755}),
])
def test_continuation_rejects_other_lineage_or_unverified_update(
        continuation_trial, filename, field, value):
    path = continuation_trial / filename
    data = json.loads(path.read_text())
    data[field] = value
    write_json(path, data)
    with pytest.raises(ValueError):
        remote_agent.continuation_entries(continuation_trial)


def test_continuation_weight_path_is_pinned(tmp_path, monkeypatch):
    root = tmp_path / "continuation"
    monkeypatch.setattr(sources, "CONTINUATION_ROOT", str(root))
    entry = dict(run=sources.CONTINUATION_RUN,
                 category=sources.CONTINUATION_CATEGORY, sha256="b" * 64)
    path, allowed = sources.checkpoint_location(tmp_path / "archive", entry)
    assert path == root / "weights" / ("b" * 64 + ".pt")
    assert allowed == root
    for patch in ({"run": "../other"}, {"run": "other-run"}, {"category": "formal"}):
        with pytest.raises(ValueError):
            sources.checkpoint_location(tmp_path / "archive", {**entry, **patch})
