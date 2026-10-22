import importlib.util
from pathlib import Path

import pytest
import torch

from native_runner.tests.test_fair_search import frame, information, model, episode, OTHER
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.consumer import EpisodeSampler, evaluate_decisions, evaluate_episodes, split_episodes
from native_runner.training.fair_search.learner import SearchLearner, decision_batch_loss
from native_runner.training.fair_search.model import save_checkpoint
from native_runner.training.fair_search.replay import ReplayPool


def test_late_episode_catches_up_and_every_row_is_seen_before_repetition(information):
    sampler = EpisodeSampler(seed=11)
    sampler.add(episode(information, identity="early", count=4))
    sampler.sample(20)
    sampler.add(episode(information, identity="late", count=5))
    early = list(sampler.counts["early"])
    sampler.sample(20)
    assert sampler.counts["early"] == early
    assert sampler.counts["late"] == [4] * 5
    sampler.sample(16)
    assert sum(sampler.counts["early"]) == sum(sampler.counts["late"]) == 28
    assert sampler.coverage()["unseen"] == 0


@pytest.mark.parametrize("balanced", [True, False])
def test_sampler_resume_reproduces_late_admission_and_sampling(information, balanced):
    records = [episode(information, identity=str(index), count=3 + index) for index in range(3)]
    sampler = EpisodeSampler(balanced=balanced, seed=12)
    for record in records[:2]:
        sampler.add(record)
    sampler.sample(17)
    restored = EpisodeSampler(balanced=balanced, seed=999)
    for record in records[:2]:
        restored.add(record)
    restored.load_state_dict(sampler.state_dict())
    for value in (sampler, restored):
        value.add(records[2])
    actual, expected = sampler.sample(79), restored.sample(79)
    assert [id(row) for row in actual] == [id(row) for row in expected]
    assert sampler.state_dict() == restored.state_dict()


def test_sampler_rejects_changed_episode_lengths_and_corrupt_cursors(information):
    sampler = EpisodeSampler()
    sampler.add(episode(information, count=5))
    sampler.sample(3)
    state = sampler.state_dict()
    state["cursor"]["game"] = 4
    with pytest.raises(ValueError, match="coverage"):
        sampler.load_state_dict(state)
    state = sampler.state_dict()
    state["identities"] = ["validation"]
    with pytest.raises(ValueError, match="identity"):
        sampler.load_state_dict(state)


def test_split_is_order_independent_and_whole_episode():
    identities = [str(index) for index in range(128)]
    split = split_episodes(identities, validation_count=16, seed=13)
    assert split == split_episodes(reversed(identities), validation_count=16, seed=13)
    assert list(split.values()).count("validation") == 16
    assert list(split.values()).count("train") == 112
    with pytest.raises(ValueError, match="distinct"):
        split_episodes(["duplicate", "duplicate"], validation_count=1, seed=13)


def test_metrics_equal_joint_reference_and_episode_weighting(model, information):
    from native_runner.tests.test_fair_learner_batch import varied_decisions

    rows = varied_decisions(information)
    records = evaluate_decisions(model, rows, batch_size=2)
    expected = decision_batch_loss(model, rows)
    assert sum(row["policy_ce"] for row in records) / len(rows) == pytest.approx(float(expected[1].detach()), abs=2e-6)
    assert sum(row["value_ce"] for row in records) / len(rows) == pytest.approx(float(expected[2].detach()), abs=2e-6)
    result = evaluate_episodes(model, [episode(information, identity="short"),
                                      episode(information, identity="long", count=4)])
    assert result["equal_episode"]["policy_ce"] == pytest.approx(
        sum(row["policy_ce"] for row in result["per_episode"].values()) / 2)


def test_continuous_consumer_inherits_optimizer_moments(tmp_path, model, information):
    from dataclasses import asdict

    source = Path(__file__).resolve().parents[2] / "tools/fit_fair_consumer.py"
    spec = importlib.util.spec_from_file_location("fit_fair_continuous_test", source)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    config = SearchConfig(batch_size=2)
    sampler = EpisodeSampler()
    sampler.add(episode(information, identity="previous", count=2))
    learner = SearchLearner(model, sampler, config=config)
    learner.sample_credit = 2
    learner.train_available(max_steps=1)
    initial = tmp_path / "initial.pt"
    identity = save_checkpoint(initial, model, learner.optimizer, generation=1,
        parent_sha256=OTHER, config_sha256=config.sha256, replay_state=ReplayPool().state_dict(),
        sample_credit=1, optimizer_steps=1)
    experiment = dict(initial_sha256=identity, seed=16, config=asdict(config), steps=0,
        patience=2, validation_interval=64, arrival_order=["train", "validation"],
        split={"train":"train","validation":"validation"}, resume_initial_optimizer=True,
        sample_remainder=1)
    episodes = {name: episode(information, identity=name, count=2) for name in experiment["arrival_order"]}
    runner.fit_variant(initial, episodes, experiment, tmp_path / "output", mode="balanced", device="cpu")
    restored = torch.load(tmp_path / "output/final.pt", weights_only=True)
    expected = learner.optimizer.state_dict()
    assert restored["sample_credit"] == 1
    assert restored["optimizer"]["param_groups"] == expected["param_groups"]
    assert expected["state"]
    for parameter, state in expected["state"].items():
        for name, value in state.items():
            assert torch.equal(restored["optimizer"]["state"][parameter][name], value)


@pytest.mark.parametrize("mode", ["stream", "uniform", "balanced"])
@pytest.mark.parametrize("temperature", [1., .5, 0.])
def test_consumer_interruption_resume_is_exact_and_validation_is_excluded(tmp_path, model, information, monkeypatch, mode, temperature):
    from dataclasses import asdict

    source = Path(__file__).resolve().parents[2] / "tools/fit_fair_consumer.py"
    spec = importlib.util.spec_from_file_location("fit_fair_consumer_test", source)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    config = SearchConfig(batch_size=4)
    initial = tmp_path / "initial.pt"
    learner = SearchLearner(model, ReplayPool(), config=config)
    identity = save_checkpoint(initial, model, learner.optimizer, generation=0,
            parent_sha256=OTHER, config_sha256=config.sha256, replay_state=learner.replay.state_dict(),
            sample_credit=0, optimizer_steps=0)
    episodes = {str(index): episode(information, identity=str(index), count=index + 2) for index in range(4)}
    experiment = dict(initial_sha256=identity, seed=16, config=asdict(config), steps=4, patience=9,
            resume_initial_optimizer=mode == "balanced",
            target_temperature=temperature, selection_metric="top_action_ce",
            validation_interval=2, arrival_order=list(episodes), arrival_counts=[1, 2, 3, 4],
            split={identity: "validation" if identity == "3" else "train" for identity in episodes})
    real_evaluate = runner.evaluate_episodes

    def checked_evaluate(model, validation):
        assert [row.episode_id for row in validation] == ["3"]
        return real_evaluate(model, validation)

    monkeypatch.setattr(runner, "evaluate_episodes", checked_evaluate)
    reference = tmp_path / "reference"
    runner.fit_variant(initial, episodes, experiment, reference, mode=mode, device="cpu")
    resumed = tmp_path / "resumed"
    original_write = runner.atomic_write

    def interrupted(path, data, **kwargs):
        original_write(path, data, **kwargs)
        if path == resumed / "latest.pt":
            state = torch.load(path, map_location="cpu", weights_only=True)
            if state["step"] == 2:
                raise RuntimeError("simulated interruption")

    with monkeypatch.context() as context:
        context.setattr(runner, "atomic_write", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            runner.fit_variant(initial, episodes, experiment, resumed, mode=mode, device="cpu")
    runner.fit_variant(initial, episodes, experiment, resumed, mode=mode, device="cpu")
    expected = torch.load(reference / "latest.pt", weights_only=True)
    actual = torch.load(resumed / "latest.pt", weights_only=True)
    assert actual["step"] == expected["step"] == 4
    assert actual["sample_credit"] == expected["sample_credit"] == 0
    assert actual["sampler"] == expected["sampler"]
    assert "3" not in actual["sampler"]["identities"]
    for key, value in actual["model"].items():
        torch.testing.assert_close(value, expected["model"][key], atol=0, rtol=0)
