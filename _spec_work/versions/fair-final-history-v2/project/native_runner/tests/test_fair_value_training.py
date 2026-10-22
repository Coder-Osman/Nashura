from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
import torch
from torch import nn

from native_runner.tests.test_fair_search import information, frame
from native_runner.training.event_checkpoint import _build_model
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4
from native_runner.training.fair_search import value_collect
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.contracts import SearchEpisode, semantic_digest
from native_runner.training.fair_search.model import SearchPolicy, load_checkpoint, save_checkpoint
from native_runner.training.fair_search.replay import ReplayPool
from native_runner.training.fair_search.value_data import (
    BASE_SHA, SAMPLING_SHA, ValueDecision, ValueEpisode, encode_episode, decode_episode,
    make_manifest, publish_episode, load_episode, terminal_label,
)
from native_runner.training.fair_search.value_train import (
    ValueTrainer, cache_episode, fresh_head, policy_digest, evaluate, acceptance,
)
from native_runner.training.fair_search.value_service import ValueCoordinator
from native_runner.training.fair_search.value_capacity import actor_device, capacity_config
from native_runner.training.fair_search.value_features import FeatureCoordinator, validate_feature


@pytest.fixture
def model():
    torch.set_num_threads(1)
    policy = _build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                          EventModelConfigV1(delay_hidden=32, cell_hidden=16), EventPolicyConfigV1())
    return SearchPolicy(policy).eval().requires_grad_(False)


def episode(information, winner=0, identity="value-00000", seed=12610000, split="train"):
    rows = tuple(ValueDecision(owner, owner, information.tick, information.frame,
                               dict(information.masks), "wait", terminal_label(winner, owner))
                 for owner in (0, 1))
    terminal = dict(ended=True, truncated=False, winner=winner, terminal_tick=200,
                    reason="native-result", rejected=0, unconfirmed=0)
    return ValueEpisode(identity, seed, split, 0, BASE_SHA, "a" * 64, SAMPLING_SHA, rows, terminal)


@pytest.mark.parametrize("winner,labels", [(0, [0, 2]), (1, [2, 0]), (None, [1, 1])])
def test_value_terminal_perspective_roundtrip(tmp_path, information, winner, labels):
    source = episode(information, winner)
    data = encode_episode(source)
    result = publish_episode(tmp_path / "game", data)
    restored = load_episode(tmp_path / "game/completed.json")
    assert [row.label for row in restored.decisions] == labels
    assert publish_episode(tmp_path / "game", data) == result
    with pytest.raises(ValueError, match="conflicting"):
        publish_episode(tmp_path / "game", encode_episode(replace(source, attempt=1)))
    with pytest.raises(ValueError):
        SearchEpisode.from_payload(source.to_payload())
    with pytest.raises(TypeError):
        ReplayPool().add(source)


@pytest.mark.parametrize("change", [dict(truncated=True), dict(ended=False), dict(rejected=1),
                                   dict(unconfirmed=1), dict(winner=True)])
def test_dirty_terminals_rejected(information, change):
    source = episode(information)
    with pytest.raises(ValueError):
        replace(source, terminal={**source.terminal, **change})


def test_wrong_label_illegal_action_checksum_and_schema(information, tmp_path):
    source = episode(information)
    with pytest.raises(ValueError, match="perspective"):
        replace(source, decisions=(replace(source.decisions[0], label=2), source.decisions[1]))
    with pytest.raises(ValueError, match="illegal"):
        replace(source.decisions[0], selected="play:7:0:0")
    raw = source.to_payload()
    raw["visits"] = {"wait": 32}
    with pytest.raises(ValueError, match="fields"):
        ValueEpisode.from_payload(raw)
    publish_episode(tmp_path / "game", encode_episode(source))
    path = tmp_path / "game/episode.pt.gz"
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        load_episode(tmp_path / "game/completed.json")


def test_manifest_is_frozen_and_disjoint():
    manifest = make_manifest("a" * 64)
    assert manifest == make_manifest("a" * 64)
    assert Counter(job["split"] for job in manifest["jobs"]) == dict(train=3072, validation=512, test=512)
    assert len({job["seed"] for job in manifest["jobs"]}) == 4096
    assert not {job["seed"] for job in manifest["jobs"]} & set(range(610000, 610064))


def test_sampling_is_full_legal_and_preserves_rng(model, information):
    frozen = {0: information, 1: replace(information, owner=1)}
    job = dict(episode_id="test", seed=19)
    before = torch.get_rng_state().clone()
    with torch.inference_mode():
        selected = value_collect.sample_actions(model, frozen, job, 0)
        repeated = value_collect.sample_actions(model, frozen, job, 0)
    assert selected == repeated
    assert torch.equal(before, torch.get_rng_state())
    assert all(frozen[owner].legal(action) for owner, action in selected.items())


def test_collector_freezes_both_views_and_keeps_cancelled_plans(monkeypatch, information):
    views = {0: information, 1: replace(information, owner=1)}
    runtime = SimpleNamespace(ended=False, closed=False, cancelled=0)
    runtime.freeze_due_information = lambda: views
    runtime.close = lambda: setattr(runtime, "closed", True)
    runtime.terminal_evidence = lambda: episode(information).terminal

    def select(model, frozen, job, index):
        assert not runtime.ended
        assert frozen == views
        return {0: "wait", 1: "wait"}

    def commit(selected, frozen):
        assert frozen == views
        runtime.cancelled += 1
        runtime.ended = True

    monkeypatch.setattr(value_collect, "sample_actions", select)
    runtime.commit_simultaneous = commit
    result = value_collect.collect_game(runtime, None, dict(episode_id="test", seed=1, split="train", attempt=0),
                                       runtime_sha256="a" * 64)
    assert len(result.decisions) == 2 and runtime.closed and runtime.cancelled == 1


def test_head_initialization_and_policy_freeze(model):
    before = policy_digest(model)
    head = fresh_head(model.wdl_head)
    assert all(parameter.requires_grad for parameter in head.parameters())
    assert not torch.equal(head[0].weight, model.wdl_head[0].weight)
    assert torch.count_nonzero(head[-1].weight) == 0
    assert policy_digest(model) == before


def toy_features(count=24):
    labels = torch.arange(count) % 3
    return dict(episode_id="toy", split="train", features=functional_one_hot(labels), labels=labels,
                owners=torch.arange(count) % 2, ticks=torch.arange(count) * 300,
                baseline=torch.zeros(count, 3))


def functional_one_hot(labels):
    return torch.nn.functional.one_hot(labels, 3).float() * 3


def test_resume_is_exact_and_overfit(tmp_path):
    torch.set_num_threads(1)
    head = fresh_head(nn.Sequential(nn.Linear(3, 16), nn.SiLU(), nn.Linear(16, 3)))
    data = [toy_features()]
    trainer = ValueTrainer(head, data, dataset_sha256="a" * 64, policy_sha256="b" * 64, batch_size=24)
    trainer.optimizer.param_groups[0]["lr"] = .01
    for _ in range(200):
        trainer.step()
    report = evaluate(trainer.head, data)
    assert report["groups"]["all"]["candidate"]["ce"] < .02
    assert acceptance(report, resamples=100)["passed"]
    trainer.save(tmp_path / "resume.pt")
    expected_loss = trainer.step()
    restored = ValueTrainer(deepcopy(head), data, dataset_sha256="a" * 64, policy_sha256="b" * 64, batch_size=24)
    restored.resume(tmp_path / "resume.pt")
    assert restored.step() == expected_loss
    for expected, actual in zip(trainer.head.parameters(), restored.head.parameters()):
        assert torch.equal(expected, actual)
    wrong = ValueTrainer(deepcopy(head), data, dataset_sha256="c" * 64, policy_sha256="b" * 64, batch_size=24)
    with pytest.raises(ValueError, match="provenance"):
        wrong.resume(tmp_path / "resume.pt")


def test_cache_matches_live_encoding_and_training_keeps_policy(tmp_path, model, information):
    source = episode(information)
    publish_episode(tmp_path / "episode", encode_episode(source))
    before = policy_digest(model)
    cache = cache_episode(model, tmp_path / "episode/completed.json", tmp_path / "features.pt",
                          expected_policy_sha=before)
    with torch.inference_mode():
        context, logits = model.encode(information.frame)
        masks = dict(information.masks)
        original = model.policy.select_action(context, **masks, sample=False).action
    torch.testing.assert_close(cache["features"][:1], context.value_context)
    trainer = ValueTrainer(fresh_head(model.wdl_head), [cache], dataset_sha256="a" * 64,
                           policy_sha256=before, batch_size=2)
    trainer.step()
    model.wdl_head.load_state_dict(trainer.head.state_dict())
    assert policy_digest(model) == before
    with torch.inference_mode():
        context, _ = model.encode(information.frame)
        actual = model.policy.select_action(context, **masks, sample=False).action
    assert torch.equal(original.act, actual.act)
    assert torch.equal(original.card_row, actual.card_row)
    optimizer = torch.optim.AdamW(model.parameters())
    sha = save_checkpoint(tmp_path / "candidate.pt", model, optimizer, generation=0,
                          parent_sha256=BASE_SHA, config_sha256=SearchConfig(particles=1).sha256,
                          replay_state=ReplayPool().state_dict(), sample_credit=0, optimizer_steps=0)
    restored, state, _ = load_checkpoint(tmp_path / "candidate.pt", expected_sha256=sha)
    assert policy_digest(restored) == before
    assert state["optimizer_steps"] == 0 and state["optimizer"]["state"] == {}
    replacement_sha = save_checkpoint(tmp_path / "candidate.pt", model, optimizer, generation=0,
                          parent_sha256=BASE_SHA, config_sha256=SearchConfig(particles=1).sha256,
                          replay_state=ReplayPool().state_dict(), sample_credit=0, optimizer_steps=0, replace=True)
    replaced, _, _ = load_checkpoint(tmp_path / "candidate.pt", expected_sha256=replacement_sha)
    assert policy_digest(replaced) == before
    assert load_episode(tmp_path / "episode/completed.json").episode_id == source.episode_id


def feature_fixture(tmp_path, model, information):
    source = episode(information)
    marker = tmp_path / "episodes" / source.episode_id / "completed.json"
    publish_episode(marker.parent, encode_episode(source))
    identity = policy_digest(model)
    destination = tmp_path / "features" / (source.episode_id + ".pt")
    cache = cache_episode(model, marker, destination, expected_policy_sha=identity)
    job = dict(episode_id=source.episode_id, seed=source.seed, split=source.split,
               runtime_sha256=source.runtime_sha256, feature_width=cache["features"].shape[1])
    return source, identity, destination, cache, job


def test_distributed_cache_validates_source_and_retry(tmp_path, model, information):
    import io
    source, identity, destination, cache, job = feature_fixture(tmp_path, model, information)
    data = destination.read_bytes()
    receipt = validate_feature(tmp_path, data, job, identity)
    assert receipt["schema"] == "fair-value-feature-manifest.v1"
    assert validate_feature(tmp_path, data, job, identity) == receipt
    for key, replacement in (("labels", cache["labels"].flip(0)),
                             ("features", cache["features"].double()),
                             ("input_sha256", ["b" * 64] * len(source.decisions))):
        changed = dict(cache, **{key: replacement})
        buffer = io.BytesIO()
        torch.save(changed, buffer)
        with pytest.raises(ValueError):
            validate_feature(tmp_path, buffer.getvalue(), job, identity)
    with pytest.raises(ValueError, match="source"):
        validate_feature(tmp_path, data, dict(job, split="test"), identity)


def test_feature_assignments_reuse_resume_and_pause(tmp_path, model, information):
    from concurrent.futures import ThreadPoolExecutor
    source, identity, destination, cache, job = feature_fixture(tmp_path, model, information)
    manifest = dict(runtime_sha256=source.runtime_sha256,
                    jobs=[dict(episode_id=source.episode_id, seed=source.seed, split=source.split)])
    plan = dict(manifest_sha256=semantic_digest(manifest), feature_width=job["feature_width"],
                policy_sha256=identity, workers=["35-gpu0", "37-gpu0"])
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "feature-plan.json").write_text(json.dumps(plan))
    payload = dict(worker="35-gpu0", healthy=True, policy_sha256=identity)
    with ThreadPoolExecutor(max_workers=1) as executor:
        service = FeatureCoordinator(tmp_path, executor)
        claimed = service.claim(payload)["job"]
        assert claimed["reuse_sha256"]
        service = FeatureCoordinator(tmp_path, executor)
        assert service.claim(payload)["job"]["episode_id"] == source.episode_id
        with pytest.raises(ValueError, match="assignment"):
            service.complete(destination.read_bytes(), source.episode_id, "37-gpu0")
        receipt = service.complete(destination.read_bytes(), source.episode_id, "35-gpu0")
        assert service.complete(destination.read_bytes(), source.episode_id, "35-gpu0") == receipt
        assert FeatureCoordinator(tmp_path, executor).status()["counts"]["complete"] == 1
        assert service.claim(payload)["finished"]
        service.failure(dict(worker="35-gpu0", error="fixture"))
        assert service.claim(payload)["paused"]


def test_coordinator_receipt_retry_and_initial_gate(tmp_path, information):
    manifest = make_manifest("a" * 64)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    service = ValueCoordinator(tmp_path)
    service.heartbeat(dict(host="34", available_gb=80, disk_gb=20, pswpin=0, pswpout=0))
    assert service.claim(dict(host="34", slot=1))["job"] is None
    job = dict(service.claim(dict(host="34", slot=0))["job"])
    source = episode(information, identity=job["episode_id"], seed=job["seed"], split=job["split"])
    data = encode_episode(source)
    receipt = service.complete(data)
    assert receipt == service.complete(data)
    assert service.counts()["complete"] == 1
    assert ValueCoordinator(tmp_path).counts()["complete"] == 1
    service.heartbeat(dict(host="34", available_gb=20, disk_gb=20, pswpin=0, pswpout=0))
    assert service.claim(dict(host="34", slot=0))["job"] is None


def test_coordinator_protocol_error_blocks_host(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(make_manifest("a" * 64)))
    service = ValueCoordinator(tmp_path)
    resource = dict(host="35", available_gb=80, disk_gb=20, pswpin=0, pswpout=0)
    service.heartbeat(resource)
    job = dict(service.claim(dict(host="35", slot=0))["job"])
    result = service.failure(dict(job=job, host="35", slot=0, transient=False, error="invalid terminal"))
    assert result["state"] == "blocked"
    assert service.heartbeat(resource)["slots"] == 0
    assert service.claim(dict(host="35", slot=0))["job"] is None


def test_episode_equal_weighting_and_validation_split_rejected():
    short = toy_features(3)
    long = toy_features(300)
    head = nn.Sequential(nn.Linear(3, 3))
    with torch.no_grad():
        head[0].weight.zero_()
        head[0].bias.copy_(torch.tensor([2., 0., -2.]))
    short["labels"] = torch.zeros(3, dtype=torch.long)
    long["labels"] = torch.full((300,), 2, dtype=torch.long)
    result = evaluate(head, [short, long])
    first = result["episodes"][0]["candidate"]["ce"]
    second = result["episodes"][1]["candidate"]["ce"]
    assert result["groups"]["all"]["candidate"]["ce"] == pytest.approx((first + second) / 2)
    with pytest.raises(ValueError, match="training split"):
        ValueTrainer(head, [{**short, "split": "validation"}], dataset_sha256="a" * 64, policy_sha256="b" * 64)


def test_collector_timeout_closes_runtime(information):
    runtime = SimpleNamespace(ended=False, closed=False)
    runtime.close = lambda: setattr(runtime, "closed", True)
    with pytest.raises(TimeoutError, match="not a draw"):
        value_collect.collect_game(runtime, None, {}, runtime_sha256="a" * 64, max_seconds=-1)
    assert runtime.closed


def high_capacity(tmp_path):
    config = capacity_config(tmp_path)
    config.update(stages=[1, 8, 16, 24],
                  limits={host: 1 if host == "34" else 24 for host in config["limits"]},
                  devices={host: [6] if host == "34" else list(range(7 if host == "35" else 8))
                           for host in config["limits"]})
    (tmp_path / "capacity.json").write_text(json.dumps(config))
    return config


def test_high_capacity_gpu_mapping_and_validation(tmp_path):
    config = high_capacity(tmp_path)
    assert capacity_config(tmp_path) == config
    assert actor_device(config, "34", 0) == 6
    assert {actor_device(config, "35", slot) for slot in range(24)} == set(range(7))
    assert {actor_device(config, "37", slot) for slot in range(24)} == set(range(8))
    with pytest.raises(ValueError, match="outside"):
        actor_device(config, "34", 1)
    config["observation_seconds"] = 30
    (tmp_path / "capacity.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="stages"):
        capacity_config(tmp_path)


def test_high_capacity_ramp_drain_and_failure_gate(tmp_path, monkeypatch):
    from native_runner.training.fair_search import value_service
    clock = [1000.]
    monkeypatch.setattr(value_service.time, "time", lambda: clock[0])
    monkeypatch.setattr(value_service.shutil, "disk_usage", lambda unused: SimpleNamespace(free=100 * 2**30))
    (tmp_path / "manifest.json").write_text(json.dumps(make_manifest("a" * 64)))
    config = high_capacity(tmp_path)
    service = ValueCoordinator(tmp_path)
    hosts = tuple(config["limits"])

    def heartbeat_all():
        for host in hosts:
            service.heartbeat(dict(host=host, available_gb=80, disk_gb=20, pswpin=0, pswpout=0))

    heartbeat_all()
    for ordinal, job in enumerate(list(service.state["jobs"].values())[:32]):
        job.update(state="complete", host=hosts[ordinal % 5], bytes=1000, rows=100)
    heartbeat_all()
    assert service.state["stage"] == 8
    assert service.claim(dict(host="35", slot=8))["job"] is None
    assert service.claim(dict(host="35", slot=7))["job"] is not None
    clock[0] += 600
    heartbeat_all()
    assert service.state["stage"] == 8
    for host, job in zip(hosts, list(service.state["jobs"].values())[40:45]):
        job.update(state="complete", host=host, bytes=1000, rows=100)
    heartbeat_all()
    assert service.state["stage"] == 16
    config["drain_hosts"] = ["35"]
    (tmp_path / "capacity.json").write_text(json.dumps(config))
    assert service.claim(dict(host="35", slot=0))["job"] is None
    assert service.heartbeat(dict(host="35", available_gb=80, disk_gb=20))["slots"] == 0
    config["drain_hosts"] = []
    (tmp_path / "capacity.json").write_text(json.dumps(config))
    clock[0] += 600
    for host, job in zip(hosts, list(service.state["jobs"].values())[50:55]):
        job.update(state="complete", host=host, bytes=1000, rows=100)
    (tmp_path / "failures").mkdir()
    (tmp_path / "failures/test.json").write_text("{}")
    heartbeat_all()
    assert service.state["stage"] == 16
    assert len(service.state["capacity_history"]) == 2


def test_parallel_upload_validates_without_holding_coordinator_lock(tmp_path, information):
    from concurrent.futures import ThreadPoolExecutor
    (tmp_path / "manifest.json").write_text(json.dumps(make_manifest("a" * 64)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        service = ValueCoordinator(tmp_path, executor=pool)
        service.heartbeat(dict(host="34", available_gb=80, disk_gb=20))
        job = dict(service.claim(dict(host="34", slot=0))["job"])
        source = episode(information, identity=job["episode_id"], seed=job["seed"], split=job["split"])
        receipt = service.complete(encode_episode(source))
        assert receipt["episode_id"] == job["episode_id"]
        assert service.counts()["complete"] == 1
        assert not service.uploads
        invalid = replace(source, model_sha256="b" * 64)
        with pytest.raises(ValueError, match="model"):
            service.complete(encode_episode(invalid))
        assert not service.uploads


def test_explicit_host_recovery_preserves_failure_evidence(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(make_manifest("a" * 64)))
    service = ValueCoordinator(tmp_path)
    service.heartbeat(dict(host="35", available_gb=80, disk_gb=20))
    job = dict(service.claim(dict(host="35", slot=0))["job"])
    service.failure(dict(job=job, host="35", slot=0, transient=True, error="upload disconnected"))
    service.state["hosts"]["35"].update(blocked=True, actor_failures=3)
    receipt = service.resume_hosts(dict(hosts=["35"], reason="parallel validator deployed"))
    assert receipt["failures"] == 1
    assert service.state["stage_failures"] == 1
    assert not service.state["hosts"]["35"]["blocked"]
    assert service.state["jobs"][job["episode_id"]]["attempt"] == 1
    job = dict(service.claim(dict(host="35", slot=0))["job"])
    service.failure(dict(job=job, host="35", slot=0, transient=False, error="protocol"))
    with pytest.raises(ValueError, match="individual repair"):
        service.resume_hosts(dict(hosts=["35"], reason="must not bypass validation"))
