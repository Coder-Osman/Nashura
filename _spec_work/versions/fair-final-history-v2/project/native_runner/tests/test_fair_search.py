from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import math
import random
import tempfile
import time
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.event_checkpoint import _build_model, save_event_checkpoint
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4
from native_runner.training.fair_search.belief import NativeConditioner, OwnInitialDraw, ReplayBeliefProvider
from native_runner.training.fair_search.config import HOG_DECK, SearchConfig
from native_runner.training.fair_search.collector import collect_episode
from native_runner.training.fair_search.contracts import InformationState, SearchDecision, SearchEpisode, PublicEvent
from native_runner.training.fair_search.coordinator import capacity_choice, generation_jobs
from native_runner.training.fair_search.evaluation import final_acceptance, paired_confidence, promotion_decision
from native_runner.training.fair_search.inference import BatchInference, LocalInference, SocketInferenceClient, SocketInferenceServer
from native_runner.training.fair_search.initialize import initialize_checkpoint, load_selfplay_checkpoint
from native_runner.training.fair_search.learner import SearchLearner, decision_loss, decode_action
from native_runner.training.fair_search.model import (
    CHECKPOINT_SCHEMA, LEGACY_CHECKPOINT_SCHEMA, NEUTRAL_VALUE_HEAD, TRANSFERRED_VALUE_HEAD,
    SearchPolicy, bootstrap, load_checkpoint, save_checkpoint,
)
from native_runner.training.fair_search.replay import ReplayPool
from native_runner.training.fair_search.search import InformationSetSearch
from native_runner.training.fair_search.storage import load_episode, save_episode, publish_terminal_episode, torch_bytes


SHA = "a" * 64
OTHER = "b" * 64


@pytest.fixture(scope="module")
def frame():
    torch.set_num_threads(1)
    return _batch(1)


@pytest.fixture
def information(frame):
    masks = dict(gate_mask=torch.tensor([[True, True]]), card_mask=torch.zeros(1, 8, dtype=torch.bool),
                 cell_masks=torch.zeros(1, 8, 576, dtype=torch.bool),
                 delay_mask=torch.zeros(1, 8, 61, dtype=torch.bool))
    masks["card_mask"][0, 0] = True
    masks["cell_masks"][0, 0, :2] = True
    masks["delay_mask"][0, 0, 1:3] = True
    return InformationState(0, 100, frame, masks)


@pytest.fixture
def model():
    policy = _build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                          EventModelConfigV1(delay_hidden=32, cell_hidden=16), EventPolicyConfigV1())
    return SearchPolicy(policy, value_head=NEUTRAL_VALUE_HEAD).eval()


def episode(info, *, identity="game", generation=1, winner=0, count=1):
    rows = tuple(SearchDecision(index, info, {"wait": 16, "play:0:0:1": 16}, "wait", SHA,
                               SearchConfig().sha256, OTHER, 0 if winner is None else (1 if winner == info.owner else -1))
                 for index in range(count))
    return SearchEpisode(identity, generation, rows, winner, 200, "native-result", SHA, (SHA, SHA))


def test_fixed_budget_configuration():
    assert SearchConfig().simulations == 32
    for change in ({"simulations": 128}, {"particles": 9}, {"cpuct": float("nan")}, {"inference_wait_ms": 21}):
        with pytest.raises(ValueError):
            SearchConfig(**change)


@pytest.mark.parametrize("action", ["play:-1:0:1", "play:0:0:0", "play:1:0:1", "play:0:576:1", "play:00:0:1", "ability:0:1"])
def test_illegal_action_rejected(information, action):
    assert not information.legal(action)
    with pytest.raises(ValueError):
        SearchDecision(0, information, {action: 32}, action, SHA, SHA, SHA, 1)


def test_information_wire_is_allowlisted(information):
    raw = information.to_payload()
    restored = InformationState.from_payload(raw)
    assert restored.sha256 == information.sha256
    assert isinstance(restored.masks["gate_mask"], torch.Tensor)
    assert restored.masks["gate_mask"].dtype == torch.bool
    assert restored.masks["gate_mask"].device.type == "cpu"
    raw["opponent_hand"] = [1, 2, 3, 4]
    with pytest.raises(ValueError):
        InformationState.from_payload(raw)


def test_private_or_future_history_rejected(information):
    with pytest.raises(ValueError):
        replace(information, public_history=(PublicEvent(1, 101, HOG_DECK[0], 100, 100),))
    with pytest.raises(ValueError):
        replace(information, own_plan={"opponent_pending": "play"})


@pytest.mark.parametrize("visits", [{"wait": 31}, {"wait": 33}, {"wait": 32.0}])
def test_budget_contract_rejected(information, visits):
    with pytest.raises(ValueError):
        SearchDecision(0, information, visits, "wait", SHA, SHA, SHA, 1)


@pytest.mark.parametrize("winner", [None, 0, 1])
def test_terminal_targets_roundtrip(tmp_path, information, winner):
    source = episode(information, winner=winner)
    manifest = save_episode(tmp_path / "game", source)
    restored = load_episode(manifest)
    assert restored.decisions[0].result == (0 if winner is None else 1 if winner == 0 else -1)
    assert restored.decisions[0].information.sha256 == information.sha256
    with pytest.raises(FileExistsError):
        save_episode(tmp_path / "game", source)


def test_incomplete_or_ppo_data_not_accepted(information):
    raw = episode(information).to_payload()
    raw["truncated"] = True
    with pytest.raises(ValueError):
        SearchEpisode.from_payload(raw)
    with pytest.raises(ValueError):
        SearchEpisode.from_payload({"schema": "event-rl-shard.v1"})
    with pytest.raises(ValueError):
        replace(episode(information), owner_sha256=(OTHER, SHA))


def test_checksum_corruption_rejected(tmp_path, information):
    manifest = save_episode(tmp_path / "game", episode(information))
    data = manifest.parent / "episode.pt.gz"
    data.write_bytes(data.read_bytes() + b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        load_episode(manifest)


@pytest.mark.parametrize("identities", [{1: SHA, 0: SHA}, (SHA, SHA), [SHA, SHA],
                                        {1: OTHER, 0: SHA}, {0: OTHER, 1: SHA}])
@pytest.mark.parametrize("winner", [None, 0, 1])
def test_collector_terminal_publication(tmp_path, information, monkeypatch, identities, winner):
    from native_runner.training.fair_search import collector

    runtime = SimpleNamespace(ended=False, closed=False)
    frozen = {0: information, 1: replace(information, owner=1)}
    runtime.freeze_due_information = lambda: frozen
    runtime.terminal_evidence = lambda: dict(ended=True, truncated=False, winner=winner,
                                            terminal_tick=200, reason="native-result", rejected=0, unconfirmed=0)
    committed = []

    def commit(selected, views):
        assert views == frozen
        committed.append(selected)
        runtime.ended = True

    runtime.commit_simultaneous = commit
    runtime.close = lambda: setattr(runtime, "closed", True)
    providers = {owner: SimpleNamespace(close=lambda: None) for owner in (0, 1)}
    result = SimpleNamespace(visits={"wait": 32}, selected="wait", seconds=0., particles=1, timings={})
    monkeypatch.setattr(collector, "InformationSetSearch", lambda *args, **kwargs:
                        SimpleNamespace(run=lambda *args, **kwargs: result))
    summary = collect_episode(runtime, providers, {0: None, 1: None}, episode_id="collect",
                              generation=1, current_sha256=SHA, owner_sha256=identities,
                              config=SearchConfig(), runtime_sha256=OTHER, destination=tmp_path / "collect")
    restored = load_episode(summary["manifest"])
    assert restored.owner_sha256 == (identities[0], identities[1])
    expected_owners = [owner for owner in (0, 1) if identities[owner] == SHA]
    assert [row.information.owner for row in restored.decisions] == expected_owners
    assert all(row.result == (0 if winner is None else 1 if row.information.owner == winner else -1)
               for row in restored.decisions)
    assert summary["decisions"] == 2 and summary["rows"] == len(expected_owners)
    assert committed == [{0: "wait", 1: "wait"}]
    assert runtime.closed


@pytest.mark.parametrize("identities", [{0: SHA}, {0: SHA, 1: "bad"}, (SHA,), (0, 1),
                                        {False: SHA, True: SHA}])
def test_collector_rejects_identities_before_search(tmp_path, identities):
    runtime = SimpleNamespace(closed=False)
    runtime.close = lambda: setattr(runtime, "closed", True)
    with pytest.raises(ValueError):
        collect_episode(runtime, {}, {}, episode_id="invalid", generation=1,
                        current_sha256=SHA, owner_sha256=identities, config=SearchConfig(),
                        runtime_sha256=OTHER, destination=tmp_path / "invalid")
    assert runtime.closed
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("failed_file", ["episode.pt.gz", "completed.json"])
def test_terminal_publication_can_retry_without_match(tmp_path, information, monkeypatch, failed_file):
    from native_runner.training.fair_search import storage
    from native_runner.training.fair_search.recover import main

    source = episode(information)
    destination = tmp_path / "retry"
    original = storage.atomic_write

    def interrupted_write(path, data, **kwargs):
        if path.name == failed_file:
            raise OSError("injected publication failure")
        return original(path, data, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(storage, "atomic_write", interrupted_write)
        with pytest.raises(OSError, match="injected"):
            save_episode(destination, source)
    recovery = tmp_path / "retry.terminal.pt.gz"
    assert recovery.exists()
    assert not (destination / "completed.json").exists()
    assert main(["--terminal-file", str(recovery), "--output", str(destination)]) == 0
    restored = load_episode(destination / "completed.json")
    assert restored.decisions[0].visits == source.decisions[0].visits
    assert restored.decisions[0].information.sha256 == information.sha256
    assert restored.owner_sha256 == (SHA, SHA)
    assert publish_terminal_episode(recovery, destination) == destination / "completed.json"
    with pytest.raises(ValueError, match="content differs"):
        publish_terminal_episode(recovery, save_episode(tmp_path / "conflict", episode(information, winner=1)).parent)


def test_terminal_recovery_rejects_corrupt_or_ppo_payload(tmp_path, information):
    import gzip

    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.storage import atomic_write

    payload = episode(information).to_payload()
    invalid = tmp_path / "invalid.pt.gz"
    raw = dict(schema="fair-search-terminal-recovery.v1", episode=payload, sha256=SHA)
    atomic_write(invalid, gzip.compress(torch_bytes(raw)))
    with pytest.raises(ValueError, match="checksum"):
        publish_terminal_episode(invalid, tmp_path / "invalid")
    raw["episode"] = {"schema": "event-rl-shard.v1"}
    raw["sha256"] = semantic_digest(raw["episode"])
    ppo = tmp_path / "ppo.pt.gz"
    atomic_write(ppo, gzip.compress(torch_bytes(raw)))
    with pytest.raises(ValueError, match="PPO"):
        publish_terminal_episode(ppo, tmp_path / "ppo")
    assert not (tmp_path / "invalid").exists()
    assert not (tmp_path / "ppo").exists()


def test_replay_retention_dedup_and_resume(information):
    pool = ReplayPool(max_generations=2, max_rows=3, seed=11)
    first = episode(information, identity="first", count=2)
    assert pool.add(first) == 2
    assert pool.add(first) == 0
    with pytest.raises(ValueError, match="conflicting"):
        pool.add(episode(information, identity="first", winner=1))
    pool.add(episode(information, identity="second", generation=2, count=2))
    assert list(pool.episodes) == ["second"]
    pool.add(episode(information, identity="third", generation=3))
    restored = ReplayPool(max_generations=2, max_rows=3)
    restored.load_state_dict(pool.state_dict())
    assert [row.index for row in restored.sample(10)] == [row.index for row in pool.sample(10)]


def test_replay_samples_episodes_not_rows(information):
    pool = ReplayPool(seed=10)
    pool.add(episode(information, identity="short"))
    pool.add(episode(information, identity="long", count=20))
    short = pool.episodes["short"].decisions[0]
    hits = sum(row is short for row in pool.sample(5000))
    assert 2300 < hits < 2700


class ToyWorld:
    def __init__(self, info, hidden=0, outcome=1):
        self.root = info
        self.hidden = hidden
        self.outcome = outcome
        self.closed = False
        self.restore_root()

    def restore_root(self):
        self.current = self.root
        self.action = None

    def root_information(self):
        return self.root

    def information(self):
        return self.current

    def apply(self, action):
        self.action = action

    def advance(self, remaining_seconds):
        self.current = replace(self.current, tick=self.current.tick + 1,
                               own_history=(*self.current.own_history, str(self.action)))

    def terminal_result(self, owner):
        if self.action is None:
            return None
        return self.outcome if self.action != "wait" else -self.outcome

    def close(self):
        self.closed = True


class ToyBelief:
    def __init__(self, hidden=0):
        self.hidden = hidden
        self.worlds = []

    def build(self, information, *, seed, limit):
        self.worlds = [ToyWorld(information, self.hidden) for _ in range(limit)]
        return self.worlds


class ToyInference:
    def predict(self, information, *, seed):
        return {"wait": .5, "play:0:0:1": .5}, 0.

    def sample(self, information, *, seed):
        return "wait"


def test_search_global32_and_hidden_invariance(information):
    left, right = ToyBelief(0), ToyBelief(999)
    first = InformationSetSearch(left, ToyInference()).run(information, seed=4)
    second = InformationSetSearch(right, ToyInference()).run(information, seed=4)
    assert sum(first.visits.values()) == 32
    assert first.visits == second.visits and first.selected == second.selected
    assert first.particles == 8
    assert all(world.closed for world in left.worlds + right.worlds)


def test_search_value_sign_and_no_root_noise_eval(information):
    result = InformationSetSearch(ToyBelief(), ToyInference()).run(information, seed=9, training=False)
    assert result.visits["play:0:0:1"] > result.visits["wait"]
    assert result.selected == "play:0:0:1"


def test_training_selection_never_chooses_zero_visit_action(information):
    result = InformationSetSearch(ToyBelief(), ToyInference()).run(information, seed=17, training=True)
    assert result.visits[result.selected] > 0


def test_search_failure_cleans_worlds(information):
    belief = ToyBelief()
    with pytest.raises(TimeoutError):
        InformationSetSearch(belief, ToyInference()).run(information, seed=1, deadline=time.monotonic() - 1)
    assert all(world.closed for world in belief.worlds)


def test_no_actual_world_fallback(information):
    provider = ReplayBeliefProvider(lambda info, seed, ordinal: ToyWorld(replace(info, tick=101)), attempts=2)
    with pytest.raises(ValueError, match="no compatible"):
        provider.build(information, seed=1, limit=8)
    assert provider.profile["rejected"] == 2


def test_particle_count_can_be_one(information):
    provider = ReplayBeliefProvider(lambda info, seed, ordinal: ToyWorld(info), attempts=1)
    result = InformationSetSearch(provider, ToyInference()).run(information, seed=8)
    assert result.particles == 1 and sum(result.visits.values()) == 32


def test_value_head_neutral_and_joint_ce_not_renormalized(model, information):
    row = SearchDecision(0, information, {"wait": 32}, "wait", SHA, SHA, SHA, 1)
    context, logits = model.encode(information.frame)
    assert torch.equal(model.value(logits), torch.zeros(1))
    _, policy, value = decision_loss(model, row)
    reference = -model.policy.evaluate_actions(context, decode_action("wait", "cpu"),
                                               **information.masks, validate=True).log_prob[0]
    torch.testing.assert_close(policy, reference)
    torch.testing.assert_close(value, torch.tensor(math.log(3)))
    assert float(policy.detach()) > 0


def test_prior_distillation_keeps_producer_identity(model, information):
    row = SearchDecision(0, information, {"wait": 32}, "wait", SHA, SHA, SHA, 1)
    teacher = SearchPolicy(_build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                                        EventModelConfigV1(delay_hidden=32, cell_hidden=16),
                                        EventPolicyConfigV1()), value_head=NEUTRAL_VALUE_HEAD).eval()
    teacher.load_state_dict(model.state_dict())
    teacher.requires_grad_(False)
    with pytest.raises(ValueError, match="producing model"):
        decision_loss(model, row, teacher=teacher, teacher_sha256=OTHER, prior_fraction=.5)
    torch.manual_seed(7)
    loss, policy, value = decision_loss(model, row, teacher=teacher,
                                        teacher_sha256=SHA, prior_fraction=.5, prior_samples=4)
    assert bool(torch.isfinite(loss))
    assert float(policy.detach()) > 0 and float(value.detach()) > 0
    loss.backward()
    assert all(parameter.grad is None for parameter in teacher.parameters())


def test_detached_value_head_does_not_update_shared_encoder(model, information):
    row = SearchDecision(0, information, {"wait": 32}, "wait", SHA, SHA, SHA, 1)
    _, _, value = decision_loss(model, row, detach_value_features=True)
    value.backward()
    assert any(parameter.grad is not None for parameter in model.wdl_head.parameters())
    assert all(parameter.grad is None for parameter in model.policy.parameters())


def test_learner_credit_duplicate_and_training(model, information):
    learner = SearchLearner(model, ReplayPool(), config=SearchConfig(batch_size=2))
    source = episode(information)
    assert learner.ingest(source) == 1
    assert learner.ingest(source) == 0
    assert learner.sample_credit == 2
    before = model.wdl_head.bias.detach().clone()
    metrics = learner.train_available()
    assert len(metrics) == 1 and learner.sample_credit == 0
    assert not torch.equal(before, model.wdl_head.bias)
    assert model.wdl_head.bias[0] > model.wdl_head.bias[2]


def test_small_dataset_overfits_terminal(model, information):
    learner = SearchLearner(model, ReplayPool(), config=SearchConfig(batch_size=1, learning_rate=.003))
    row = episode(information).decisions[0]
    before = float(decision_loss(model, row)[0].detach())
    learner.replay.add(episode(information))
    learner.sample_credit = 16
    learner.train_available()
    after = float(decision_loss(model, row)[0].detach())
    assert after < before * .7


@pytest.mark.parametrize("value_head", [NEUTRAL_VALUE_HEAD, TRANSFERRED_VALUE_HEAD])
def test_checkpoint_roundtrip_and_rng_resume(tmp_path, model, information, value_head):
    model = SearchPolicy(model.policy, value_head=value_head).eval()
    learner = SearchLearner(model, ReplayPool(), config=SearchConfig(batch_size=2))
    learner.ingest(episode(information))
    learner.train_available()
    path = tmp_path / "search.pt"
    identity = save_checkpoint(path, model, learner.optimizer, generation=1, parent_sha256=OTHER,
                               config_sha256=learner.config.sha256, replay_state=learner.replay.state_dict(),
                               sample_credit=learner.sample_credit, optimizer_steps=learner.optimizer_steps)
    expected_random = random.random()
    expected_tensor = torch.rand(3)
    loaded, payload, sha = load_checkpoint(path, expected_sha256=identity, resume=True)
    assert sha == identity and random.random() == expected_random
    assert torch.equal(torch.rand(3), expected_tensor)
    restored = SearchLearner(loaded, ReplayPool(), config=learner.config)
    restored.resume(payload)
    assert restored.optimizer_steps == learner.optimizer_steps
    assert restored.replay.rows == 1
    assert loaded.value_head_architecture == value_head
    for name, parameter in model.state_dict().items():
        torch.testing.assert_close(loaded.state_dict()[name], parameter)
    with pytest.raises(ValueError, match="identity"):
        load_checkpoint(path, expected_sha256=SHA)
    learner.sample_credit = restored.sample_credit = 2
    learner.train_available()
    restored.train_available()
    for name, parameter in model.state_dict().items():
        torch.testing.assert_close(loaded.state_dict()[name], parameter)


def test_bootstrap_does_not_copy_ppo_optimizer(tmp_path, model):
    parent = tmp_path / "parent.pt"
    identity = save_event_checkpoint(parent, model.policy, extra={"event_ppo_training": {"iterations": 1755}})
    loaded, sha = bootstrap(parent, expected_sha256=identity)
    assert sha == identity and loaded.value_head_architecture == TRANSFERRED_VALUE_HEAD
    torch.testing.assert_close(loaded.wdl_head[0].weight, model.policy.backbone.value_head[0].weight)
    with pytest.raises(ValueError, match="not PPO"):
        load_checkpoint(parent)


def test_transferred_value_head_preserves_legacy_log_odds_and_policy(model, information):
    old = model.policy.backbone.value_head
    original = {name: value.clone() for name, value in model.policy.state_dict().items()}
    transferred = SearchPolicy(model.policy).eval()
    vectors = torch.randn(128, old[0].in_features)
    scalar = old(vectors).squeeze(-1)
    logits = transferred.wdl_head(vectors)
    torch.testing.assert_close(logits[:, 0], scalar * .5)
    torch.testing.assert_close(logits[:, 1], torch.zeros_like(scalar))
    torch.testing.assert_close(logits[:, 2], scalar * -.5)
    value = transferred.value(logits)
    assert torch.equal(torch.sign(value), torch.sign(scalar))
    assert bool((value[scalar.argsort()].diff() >= -1e-6).all())
    for name, parameter in original.items():
        torch.testing.assert_close(model.policy.state_dict()[name], parameter, rtol=0, atol=0)
    assert transferred.wdl_head[0].weight.data_ptr() != old[0].weight.data_ptr()
    context, logits = transferred.encode(information.frame)
    torch.testing.assert_close(logits[:, 0] - logits[:, 2], context.value)


def test_transferred_value_gradients_do_not_update_legacy_head(model):
    transferred = SearchPolicy(model.policy)
    vectors = torch.randn(4, model.policy.backbone.value_head[0].in_features)
    torch.nn.functional.cross_entropy(transferred.wdl_head(vectors), torch.tensor([0, 1, 2, 0])).backward()
    assert all(parameter.grad is None for parameter in model.policy.parameters())
    assert all(parameter.grad is not None and bool(torch.isfinite(parameter.grad).all())
               for parameter in transferred.wdl_head.parameters())


@pytest.mark.parametrize("corruption", ["shape", "nonfinite", "unknown"])
def test_transferred_value_rejects_invalid_heads(model, corruption):
    if corruption == "shape":
        model.policy.backbone.value_head = torch.nn.Linear(model.policy.backbone.config.lstm_hidden_dim, 1)
    elif corruption == "nonfinite":
        with torch.no_grad():
            model.policy.backbone.value_head[0].bias[0] = float("nan")
    with pytest.raises(ValueError):
        SearchPolicy(model.policy, value_head="unknown" if corruption == "unknown" else TRANSFERRED_VALUE_HEAD)


def test_legacy_v1_checkpoint_keeps_linear_head_and_optimizer(tmp_path, model, information):
    learner = SearchLearner(model, ReplayPool(), config=SearchConfig(batch_size=2))
    learner.ingest(episode(information))
    learner.train_available()
    path = tmp_path / "search-v1.pt"
    save_checkpoint(path, model, learner.optimizer, generation=1, parent_sha256=OTHER,
                    config_sha256=learner.config.sha256, replay_state=learner.replay.state_dict(),
                    sample_credit=learner.sample_credit, optimizer_steps=learner.optimizer_steps)
    payload = torch.load(path, weights_only=True)
    payload["schema"] = LEGACY_CHECKPOINT_SCHEMA
    del payload["value_head"]
    path.write_bytes(torch_bytes(payload))
    loaded, state, _ = load_checkpoint(path)
    assert isinstance(loaded.wdl_head, torch.nn.Linear)
    assert loaded.value_head_architecture == NEUTRAL_VALUE_HEAD
    for name, parameter in model.state_dict().items():
        torch.testing.assert_close(loaded.state_dict()[name], parameter, rtol=0, atol=0)
    restored = SearchLearner(loaded, ReplayPool(), config=learner.config)
    restored.resume(state)
    learner.sample_credit = restored.sample_credit = 2
    learner.train_available()
    restored.train_available()
    for name, parameter in model.state_dict().items():
        torch.testing.assert_close(loaded.state_dict()[name], parameter)


@pytest.mark.parametrize("corruption", ["architecture", "missing", "weights"])
def test_v2_checkpoint_rejects_value_contract_mismatch(tmp_path, model, corruption):
    learner = SearchLearner(model, ReplayPool())
    path = tmp_path / "invalid.pt"
    save_checkpoint(path, model, learner.optimizer, generation=0, parent_sha256=OTHER,
                    config_sha256=learner.config.sha256, replay_state=learner.replay.state_dict(),
                    sample_credit=0, optimizer_steps=0)
    payload = torch.load(path, weights_only=True)
    if corruption == "architecture":
        payload["value_head"] = "unknown"
    elif corruption == "missing":
        del payload["value_head"]
    else:
        payload["value_head"] = TRANSFERRED_VALUE_HEAD
    path.write_bytes(torch_bytes(payload))
    with pytest.raises((ValueError, RuntimeError)):
        load_checkpoint(path)


def test_initialized_checkpoint_has_new_identity_and_empty_training_state(tmp_path, model):
    parent = tmp_path / "parent.pt"
    parent_sha = save_event_checkpoint(parent, model.policy,
                                       extra={"event_ppo_training": {"iterations": 1755, "optimizer": {"old": True}}})
    destination = tmp_path / "initial.pt"
    config = SearchConfig(particles=1)
    created, identity = load_selfplay_checkpoint(parent, initialization_path=destination, config=config,
                                                 expected_parent_sha256=parent_sha)
    assert identity != parent_sha and created.value_head_architecture == TRANSFERRED_VALUE_HEAD
    loaded, state, actual = load_checkpoint(destination, expected_sha256=identity)
    assert actual == identity and state["parent_sha256"] == parent_sha
    assert state["schema"] == CHECKPOINT_SCHEMA and state["generation"] == 0
    assert state["optimizer"]["state"] == {}
    assert state["sample_credit"] == state["optimizer_steps"] == 0
    assert state["replay_state"]["episodes"] == []
    assert "event_ppo_training" not in state["policy"]
    for name, parameter in created.state_dict().items():
        torch.testing.assert_close(loaded.state_dict()[name], parameter, rtol=0, atol=0)
    again, same_identity = load_selfplay_checkpoint(destination, initialization_path=tmp_path / "unused.pt", config=config)
    assert same_identity == identity and not (tmp_path / "unused.pt").exists()
    assert again.value_head_architecture == TRANSFERRED_VALUE_HEAD
    with pytest.raises(FileExistsError):
        initialize_checkpoint(parent, destination, config=config, expected_parent_sha256=parent_sha)
    with pytest.raises(ValueError, match="identity"):
        initialize_checkpoint(parent, tmp_path / "wrong-parent.pt", config=config, expected_parent_sha256=SHA)
    with pytest.raises(ValueError, match="configuration"):
        load_selfplay_checkpoint(destination, initialization_path=tmp_path / "unused.pt", config=SearchConfig(particles=2))


def test_local_inference_uses_transferred_wdl_value(model, information):
    transferred = SearchPolicy(model.policy).eval()
    with torch.inference_mode():
        _, logits = transferred.encode(information.frame)
        expected = float(transferred.value(logits)[0])
    backend = LocalInference(transferred, SHA, config=SearchConfig(candidates=8, initial_candidates=4))
    _, value = backend.predict(information, seed=12)
    assert value == pytest.approx(expected) and -1 <= value <= 1


@pytest.mark.parametrize("legacy_parent", [True, False])
def test_benchmark_freezes_one_checkpoint_for_all_trials(tmp_path, monkeypatch, legacy_parent):
    from native_runner.training.fair_search import benchmark_roots

    source = tmp_path / "source.pt"
    output = tmp_path / "benchmark"
    prepared = []
    commands = []

    def prepare(path, *, initialization_path, config):
        assert path == source and config == SearchConfig(particles=1)
        prepared.append(path)
        if legacy_parent:
            initialization_path.write_bytes(b"frozen migrated checkpoint")
        return None, SHA

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=json.dumps(dict(
            root_sha256=OTHER, checkpoint_sha256=SHA, config_sha256=SearchConfig(particles=1).sha256,
            runtime_sha256=OTHER, visits={"wait": 32}, selected="wait", particles=1,
            trace_sha256=[SHA], search_only="--search-only" in command,
            seconds=1. if "--search-only" in command else 2.)))

    monkeypatch.setattr(benchmark_roots, "load_selfplay_checkpoint", prepare)
    monkeypatch.setattr(benchmark_roots.subprocess, "run", run)
    assert benchmark_roots.main([
        "--checkpoint", str(source), "--output", str(output), "--puct-root", str(tmp_path),
        "--replay-root", str(tmp_path / "root.pt"), "--main-port", "30001", "--belief-port", "30002",
        "--pairs", "2",
    ]) == 0
    assert prepared == [source] and len(commands) == 4
    expected = output / "initial-checkpoint.pt" if legacy_parent else source
    assert all(command[command.index("--checkpoint") + 1] == str(expected) for command in commands)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["checkpoint_sha256"] == SHA and summary["root_speedup"] == 2.


def test_generation_allocation_and_owner_balance():
    jobs = generation_jobs(1, SHA, [OTHER])
    assert len(jobs) == 128 and all(job["kind"] == "self" for job in jobs)
    jobs = generation_jobs(3, SHA, [OTHER, SHA])
    history = [job for job in jobs if job["kind"] == "history"]
    assert len(history) == 32
    assert sum(job["owner_sha256"][0] == SHA for job in history) == 16
    assert len({job["match_seed"] for job in jobs}) == 128


def test_capacity_memory_and_failure_gates():
    safe = dict(actors=1, available_bytes=40 * 1024**3, swap_in_bytes=0, swap_out_bytes=0,
                completed_games=4, failed_games=0, seconds=100.)
    assert capacity_choice([safe, {**safe, "actors": 4, "failed_games": 1}]) == 1
    with pytest.raises(ValueError):
        capacity_choice([{**safe, "available_bytes": 31 * 1024**3}])


def test_promotion_requires_complete_independent_pairs():
    assert not promotion_decision([(0., 0.)] * 200, [])["promote"]
    assert promotion_decision([(1., 1.)] * 200, [(1., 1.)] * 1000)["promote"]
    assert final_acceptance([(1., 1.)] * 1000)["passed"]
    with pytest.raises(ValueError):
        paired_confidence([(1.,)])


class EchoBackend:
    def __init__(self, identity):
        self.identity = identity

    def infer_batch(self, requests):
        return [dict(action=f"seed:{request['seed']}") for request in requests]


def test_microbatch_tail_order_and_model_separation(information):
    service = BatchInference({SHA: EchoBackend(SHA), OTHER: EchoBackend(OTHER)},
                             config=SearchConfig(inference_wait_ms=20))
    try:
        futures = [service.submit(SHA if index % 2 else OTHER, "sample", information, index) for index in range(8)]
        assert [future.result(timeout=5)["action"] for future in futures] == [f"seed:{index}" for index in range(8)]
        assert service.profile["max_batch"] >= 2
        with pytest.raises(ValueError):
            service.submit("c" * 64, "sample", information, 1)
    finally:
        service.close()
    assert not service.thread.is_alive()


def test_socket_inference_identity_and_shutdown(information):
    batch = BatchInference({SHA: EchoBackend(SHA)})
    temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="fair-test-")
    server = SocketInferenceServer(temporary.name + "/service", batch)
    client = SocketInferenceClient(server.path, SHA)
    try:
        assert client.sample(information, seed=9) == "seed:9"
        invalid = SocketInferenceClient(server.path, OTHER)
        try:
            with pytest.raises(RuntimeError):
                invalid.sample(information, seed=1)
        finally:
            invalid.socket.close()
    finally:
        client.socket.close()
        server.close()
        temporary.cleanup()


def test_real_local_inference_request_rng_isolation(model, information):
    backend = LocalInference(model, SHA, config=SearchConfig(candidates=8, initial_candidates=4))
    before = torch.get_rng_state().clone()
    first = backend.predict(information, seed=12)
    second = backend.predict(information, seed=12)
    assert first == second and torch.equal(before, torch.get_rng_state())
    assert len(first[0]) <= 8 and all(information.legal(action) for action in first[0])
    with torch.no_grad():
        model.wdl_head.bias.add_(1)
    with pytest.raises(ValueError, match="changed"):
        backend.predict(information, seed=12)
