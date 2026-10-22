from dataclasses import asdict, replace
import hashlib
import json

import pytest
import torch

from native_runner.tests.test_fair_search import frame, information, model, OTHER
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.contracts import SearchDecision, SearchEpisode
from native_runner.training.fair_search.coordinator import GenerationCoordinator, generation_jobs
from native_runner.training.fair_search.learner import SearchLearner
from native_runner.training.fair_search.model import load_checkpoint, save_checkpoint
from native_runner.training.fair_search.replay import ReplayPool
from native_runner.training.fair_search.storage import save_episode
from native_runner.training.fair_search.streaming import StreamingGeneration


def initialize(root, model, config, *, generation=1, history=()):
    learner = SearchLearner(model, ReplayPool(max_rows=config.replay_rows), config=config)
    identity = save_checkpoint(root / "initial.pt", model, learner.optimizer, generation=0,
                               parent_sha256=OTHER, config_sha256=config.sha256,
                               replay_state=learner.replay.state_dict(), sample_credit=0, optimizer_steps=0)
    manifest = dict(config=asdict(config), config_sha256=config.sha256, checkpoint_sha256=identity,
                    history=list(history), jobs=generation_jobs(generation, identity, history, config=config))
    return manifest


def publish(root, manifest, information, index=0, *, count=1, validated=True):
    job = manifest["jobs"][index]
    owner = job["owner_sha256"].index(job["current_sha256"])
    information = replace(information, owner=owner)
    decisions = tuple(SearchDecision(
        ordinal, information, {"wait": 16, "play:0:0:1": 16}, "wait", job["current_sha256"],
        manifest["config_sha256"], OTHER, 1) for ordinal in range(count))
    episode = SearchEpisode(job["episode_id"], job["generation"], decisions, owner, 200,
                            "native-result", job["current_sha256"], tuple(job["owner_sha256"]))
    path = save_episode(root / "replay" / episode.episode_id, episode)
    if validated:
        (path.parent / "validated.json").write_text(json.dumps(dict(
            episode_id=episode.episode_id, rows=count, attempt=0, runtime_sha256=OTHER)))
    return path


def test_stream_trains_before_generation_completes_and_deduplicates(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not stream.tick()
    assert stream.learner.optimizer_steps == 1
    assert stream.durable_steps == 1 and stream.learner.sample_credit == 0
    assert len(stream.seen) == 1
    assert not stream.ready_path.exists()
    assert not stream.tick()
    assert stream.learner.optimizer_steps == 1
    assert hashlib.sha256((tmp_path / "initial.pt").read_bytes()).hexdigest() == manifest["checkpoint_sha256"]


def test_independent_schedule_trains_resumes_and_rejects_seed_change(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    old_seeds = {job["match_seed"] for job in manifest["jobs"]}
    manifest["schedule_seed"] = 2026101202
    manifest["jobs"] = generation_jobs(1, manifest["checkpoint_sha256"], [],
                                       seed=manifest["schedule_seed"], config=config)
    assert not old_seeds & {job["match_seed"] for job in manifest["jobs"]}
    publish(tmp_path, manifest, information)
    original = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not original.tick()
    resumed = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert resumed.learner.optimizer_steps == 1 and len(resumed.seen) == 1
    assert not resumed.tick()
    manifest["schedule_seed"] += 1
    with pytest.raises(ValueError, match="session differs"):
        StreamingGeneration(tmp_path, manifest, config, device="cpu")


def test_stream_only_accepts_validated_complete_episodes(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    path = publish(tmp_path, manifest, information, validated=False)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not stream.tick() and not stream.seen
    (path.parent / "validated.json").write_text(json.dumps(dict(
        episode_id="wrong", rows=1, attempt=0, runtime_sha256=OTHER)))
    with pytest.raises(ValueError, match="validated episode identity"):
        stream.tick()
    assert stream.learner.optimizer_steps == 0


def test_stream_crash_resume_replays_uncommitted_batch_exactly(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information, count=3)
    original = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not original.tick()
    assert original.learner.optimizer_steps == 1 and original.durable_steps == 0
    resumed = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert resumed.learner.optimizer_steps == 0 and resumed.learner.sample_credit == 6
    assert not resumed.tick()
    for name, parameter in original.learner.model.state_dict().items():
        torch.testing.assert_close(resumed.learner.model.state_dict()[name], parameter, rtol=0, atol=0)
    assert resumed.learner.replay.rng.getstate() == original.learner.replay.rng.getstate()
    resumed.tick()
    resumed.tick()
    assert resumed.learner.optimizer_steps == 3 and resumed.learner.sample_credit == 0
    again = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert again.learner.optimizer_steps == 3 and again.durable_steps == 3
    assert not again.tick() and again.learner.optimizer_steps == 3


def test_stream_discovers_new_episode_between_batches(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information, count=3)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    stream.tick()
    publish(tmp_path, manifest, information, index=1)
    stream.tick()
    assert len(stream.seen) == 2 and stream.learner.optimizer_steps == 2
    assert stream.learner.sample_credit == 4


def test_stream_ingests_one_episode_per_tick(tmp_path, model, information):
    config = SearchConfig(batch_size=4)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information, count=1)
    publish(tmp_path, manifest, information, index=1, count=1)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not stream.tick()
    assert len(stream.seen) == 1
    assert stream.learner.optimizer_steps == 0
    assert not stream.tick()
    assert len(stream.seen) == 2
    assert stream.learner.optimizer_steps == 1


@pytest.mark.parametrize("field", ["parent_sha256", "generation", "config_sha256"])
def test_stream_rejects_foreign_resume_checkpoint(tmp_path, model, information, field):
    from native_runner.training.fair_search.storage import torch_bytes

    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    stream.tick()
    state = torch.load(stream.checkpoint, weights_only=True)
    state[field] = 9 if field == "generation" else OTHER
    stream.checkpoint.write_bytes(torch_bytes(state))
    with pytest.raises(ValueError):
        StreamingGeneration(tmp_path, manifest, config, device="cpu")


def test_stream_rejects_changed_manifest_on_resume(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information)
    StreamingGeneration(tmp_path, manifest, config, device="cpu").tick()
    manifest["jobs"][0]["match_seed"] += 1
    with pytest.raises(ValueError, match="session differs"):
        StreamingGeneration(tmp_path, manifest, config, device="cpu")


def test_stream_seals_only_full_generation_and_recovers_publication(tmp_path, model, information, monkeypatch):
    from native_runner.training.fair_search import streaming

    config = SearchConfig(batch_size=256, replay_rows=256)
    manifest = initialize(tmp_path, model, config)
    first = publish(tmp_path, manifest, information, count=128)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not stream.tick() and stream.learner.optimizer_steps == 1
    with pytest.raises(ValueError, match="every distinct planned"):
        stream.coordinator.finalize_generation(1, [first])
    for index in range(1, 128):
        publish(tmp_path, manifest, information, index)
    original_publish = streaming.publish_status

    def interrupt_ready(path, value):
        if path == stream.ready_path:
            raise OSError("injected ready publication failure")
        original_publish(path, value)

    with monkeypatch.context() as context:
        context.setattr(streaming, "publish_status", interrupt_ready)
        for _ in range(126):
            assert not stream.tick()
        with pytest.raises(OSError, match="injected"):
            stream.tick()
    candidate = stream.training / "candidate-g0001.pt"
    assert candidate.exists() and not stream.ready_path.exists()
    assert len(stream.seen) == 128 and len(stream.learner.replay.episodes) == 128
    resumed = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert len(resumed.seen) == 128
    while not resumed.tick():
        pass
    ready = json.loads(resumed.ready_path.read_text())
    assert ready["promoted"] is False and ready["optimizer_steps"] == 1
    assert ready["sample_credit"] == 254
    assert resumed.tick()
    loaded, state, identity = load_checkpoint(candidate, expected_sha256=ready["sha256"])
    assert identity == resumed.checkpoint_sha256
    assert state["sample_credit"] == 254 and state["optimizer_steps"] == 1
    candidate.write_bytes(candidate.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="readiness differs"):
        resumed.tick()


def test_generation_three_uses_manifest_history(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config, generation=3, history=[OTHER])
    index = next(index for index, job in enumerate(manifest["jobs"]) if job["kind"] == "history")
    publish(tmp_path, manifest, information, index)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert not stream.tick() and stream.learner.optimizer_steps == 1


def test_batch_generation_validates_barrier_before_updating(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    path = publish(tmp_path, manifest, information)
    learner = SearchLearner(model, ReplayPool(), config=config)
    coordinator = GenerationCoordinator(tmp_path / "training", learner, champion_path=tmp_path / "initial.pt",
                                         champion_sha256=manifest["checkpoint_sha256"])
    with pytest.raises(ValueError, match="every distinct planned"):
        coordinator.train_generation(1, [path])
    assert learner.optimizer_steps == 0 and learner.replay.rows == 0


def test_checkpoint_write_failure_keeps_previous_checkpoint(tmp_path, model, information, monkeypatch):
    from native_runner.training.fair_search import storage

    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    stream.tick()
    previous = stream.checkpoint.read_bytes()

    def fail_replace(*args):
        raise OSError("injected atomic replacement failure")

    with monkeypatch.context() as context:
        context.setattr(storage.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected"):
            stream.coordinator.save_stream_state(1, stream.checkpoint)
    assert stream.checkpoint.read_bytes() == previous
    resumed = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert resumed.learner.optimizer_steps == 1


def test_legacy_stream_migrates_without_resetting_optimizer_or_credit(tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information, count=3)
    original = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    original.tick()
    save_checkpoint(original.checkpoint, original.learner.model, original.learner.optimizer,
                    generation=1, parent_sha256=manifest["checkpoint_sha256"], config_sha256=config.sha256,
                    replay_state=original.learner.replay.state_dict(), sample_credit=original.learner.sample_credit,
                    optimizer_steps=original.learner.optimizer_steps, replace=True)
    resumed = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert resumed.learner.optimizer_steps == 1 and resumed.learner.sample_credit == 4
    assert resumed.learner.replay.rng.getstate() == original.learner.replay.rng.getstate()
    for parameter_id, state in original.learner.optimizer.state_dict()["state"].items():
        for name, value in state.items():
            torch.testing.assert_close(resumed.learner.optimizer.state_dict()["state"][parameter_id][name], value,
                                       atol=0, rtol=0)
    resumed.save()
    raw = torch.load(resumed.checkpoint, weights_only=True)
    assert raw["replay_state"]["schema"] == "fair-search-replay.v2"
    assert "decisions" not in raw["replay_state"]["episodes"][0]
    again = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    assert again.learner.sample_credit == 4 and again.learner.optimizer_steps == 1
    original.tick()
    again.tick()
    for name, value in original.learner.model.state_dict().items():
        torch.testing.assert_close(again.learner.model.state_dict()[name], value, atol=0, rtol=0)


def test_reference_checkpoint_supports_standalone_inference_but_requires_blobs_to_resume(
        tmp_path, model, information):
    config = SearchConfig(batch_size=2)
    manifest = initialize(tmp_path, model, config)
    publish(tmp_path, manifest, information)
    stream = StreamingGeneration(tmp_path, manifest, config, device="cpu")
    stream.tick()
    standalone = tmp_path / "standalone.pt"
    standalone.write_bytes(stream.checkpoint.read_bytes())
    loaded, _, _ = load_checkpoint(standalone)
    with torch.no_grad():
        torch.testing.assert_close(loaded.encode(information.frame)[1], stream.learner.model.encode(information.frame)[1])
    with pytest.raises(FileNotFoundError):
        load_checkpoint(standalone, resume=True)
