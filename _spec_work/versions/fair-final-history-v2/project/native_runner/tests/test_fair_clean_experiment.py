from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_search import frame, information, model, episode, SHA, OTHER
from native_runner.training.fair_search.clean_experiment import export_initial, identical_state, load_cohort
from native_runner.training.fair_search.collector import collect_episode, independent_visit_action
from native_runner.training.fair_search.config import SearchConfig, load_search_config
from native_runner.training.fair_search.learner import SearchLearner
from native_runner.training.fair_search.model import load_checkpoint, save_checkpoint
from native_runner.training.fair_search.replay import ReplayPool
from native_runner.training.fair_search.storage import save_episode, load_episode


def test_explicit_config_roundtrip_and_unknown_fields(tmp_path):
    config = SearchConfig(particles=1, root_noise=0., late_temperature=1.)
    path = tmp_path / "search.json"
    path.write_text(json.dumps(asdict(config)))
    assert load_search_config(path) == config
    path.write_text(json.dumps(dict(asdict(config), unexpected=True)))
    with pytest.raises(TypeError):
        load_search_config(path)


def test_clean_export_preserves_parameters_buffers_and_clears_training(tmp_path, model):
    old_config = SearchConfig(particles=1)
    learner = SearchLearner(model, ReplayPool(), config=old_config)
    source = tmp_path / "source.pt"
    identity = save_checkpoint(source, model, learner.optimizer, generation=2, parent_sha256=OTHER,
        config_sha256=old_config.sha256, replay_state=learner.replay.state_dict(), sample_credit=17, optimizer_steps=8)
    config = replace(old_config, root_noise=0., late_temperature=1.)
    origin = export_initial(source, tmp_path / "clean", config, expected_sha=identity)
    restored, payload, new_identity = load_checkpoint(tmp_path / "clean/initial.pt")
    assert new_identity != identity
    assert origin["source_sha256"] == identity
    assert identical_state(model, restored)
    assert payload["config_sha256"] == config.sha256
    assert payload["sample_credit"] == payload["optimizer_steps"] == 0
    assert not payload["optimizer"]["state"]
    assert export_initial(source, tmp_path / "clean", config, expected_sha=identity) == origin


@pytest.mark.parametrize("tick", [100, 1200, 4000])
@pytest.mark.parametrize("winner", [None, 0, 1])
def test_clean_collector_isolates_views_and_samples_untempered_visits(tmp_path, information, monkeypatch, tick, winner):
    from native_runner.training.fair_search import collector
    views = {owner: replace(information, owner=owner, tick=tick) for owner in (0, 1)}
    runtime = SimpleNamespace(ended=False, closed=False)
    runtime.freeze_due_information = lambda: views
    calls, events = [], []
    visits = {"wait": 8, "play:0:0:1": 24}
    def search(view, **kwargs):
        assert runtime.ended is False
        assert kwargs["training"] is False
        assert view is views[view.owner]
        calls.append(view.owner)
        return SimpleNamespace(visits=visits, selected="ignored", seconds=0., particles=1, timings={})
    monkeypatch.setattr(collector, "InformationSetSearch", lambda *args, **kwargs: SimpleNamespace(run=search))
    def commit(selected, frozen):
        assert calls == [0, 1]
        assert frozen is views
        assert selected == {owner: independent_visit_action(visits, "clean", owner, 0)[0] for owner in (0, 1)}
        runtime.ended = True
    runtime.commit_simultaneous = commit
    runtime.close = lambda: setattr(runtime, "closed", True)
    runtime.terminal_evidence = lambda: dict(ended=True, truncated=False, winner=winner,
        terminal_tick=tick + 100, reason="native-result", rejected=0, unconfirmed=0)
    result = collect_episode(runtime, {0: None, 1: None}, {0: None, 1: None}, episode_id="clean", generation=1,
        current_sha256=SHA, owner_sha256=(SHA, SHA), config=SearchConfig(particles=1, root_noise=0., late_temperature=1.),
        runtime_sha256=OTHER, destination=tmp_path / "game", independent_visit_sampling=True, progress=events.append)
    restored = load_episode(result["manifest"])
    assert len(restored.decisions) == 2
    assert all(row.visits == visits for row in restored.decisions)
    assert [row.result for row in restored.decisions] == [0 if winner is None else 1 if winner == owner else -1 for owner in (0, 1)]
    assert all(event["action_seed"] is not None for event in events)
    assert runtime.closed


@pytest.mark.parametrize("total,train_count", [(128,112), (124,109)])
def test_clean_cohort_budget_uses_train_only_and_rejects_old_labels(tmp_path, information, total, train_count):
    config = SearchConfig(particles=1, root_noise=0., late_temperature=1.)
    jobs, split = [], {}
    for index in range(total):
        identity = f"game-{index}"
        split[identity] = "train" if index < train_count else "validation"
        original = episode(information, identity=identity, count=2 if index < train_count else 4)
        clean = replace(original, decisions=tuple(replace(row, config_sha256=config.sha256) for row in original.decisions))
        directory = tmp_path / "replay" / identity
        save_episode(directory, clean)
        (directory / "validated.json").write_text(json.dumps(dict(episode_id=identity, attempt=0,
            rows=len(clean.decisions), runtime_sha256=OTHER)))
        jobs.append(dict(episode_id=identity,generation=1,owner_sha256=[SHA,SHA]))
    manifest = dict(config=asdict(config),config_sha256=config.sha256,jobs=jobs,split=split,
                    checkpoint_sha256=SHA,action_sampling="independent-visits.v1")
    if total != 128:
        manifest["cohort_counts"] = dict(total=total,train=train_count,validation=total-train_count)
    episodes, experiment = load_cohort(tmp_path, manifest)
    assert len(episodes) == total
    assert experiment["training_rows"] == 2 * train_count
    assert (experiment["steps"],experiment["sample_remainder"]) == divmod(4 * train_count,config.batch_size)
    with pytest.raises(ValueError, match="contract"):
        load_cohort(tmp_path, dict(manifest,cohort_counts=dict(total=total+1,train=train_count,validation=total-train_count)))
    with pytest.raises(ValueError, match="contract"):
        load_cohort(tmp_path, dict(manifest,config=asdict(replace(config,root_noise=.25))))


def test_remainder_budget_finishes_and_saves_final_resume_state(tmp_path, model, information):
    source = Path(__file__).resolve().parents[2] / "tools/fit_fair_consumer.py"
    spec = importlib.util.spec_from_file_location("clean_fit_test", source)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    config = SearchConfig(batch_size=4)
    learner = SearchLearner(model, ReplayPool(), config=config)
    initial = tmp_path / "initial.pt"
    identity = save_checkpoint(initial,model,learner.optimizer,generation=0,parent_sha256=OTHER,
        config_sha256=config.sha256,replay_state=learner.replay.state_dict(),sample_credit=0,optimizer_steps=0)
    episodes = {key: episode(information,identity=key,count=5) for key in ("train","validation")}
    experiment = dict(initial_sha256=identity,seed=20261013,config=asdict(config),steps=2,sample_remainder=2,
        patience=4,validation_interval=64,arrival_order=list(episodes),split={"train":"train","validation":"validation"})
    result = runner.fit_variant(initial,episodes,experiment,tmp_path/"fit",mode="balanced",device="cpu")
    final = torch.load(tmp_path/"fit/final.pt",weights_only=True)
    assert result["sample_remainder"] == final["sample_credit"] == 2
    assert final["step"] == 2
    assert [record["step"] for record in final["history"]] == [0,2]
    assert list(final["sampler"]["counts"]) == ["train"]


def test_policy_evaluation_freezes_both_sides_before_commit(monkeypatch, model, information):
    from native_runner.training.fair_search import evaluate_policy
    views = {owner: replace(information, owner=owner) for owner in (0,1)}
    runtime = SimpleNamespace(ended=False)
    runtime.freeze_due_information = lambda: views
    calls = []
    original = model.policy.select_action
    def select(*args, **kwargs):
        assert not runtime.ended and kwargs["sample"] is False
        calls.append("select")
        return original(*args, **kwargs)
    monkeypatch.setattr(model.policy,"select_action",select)
    def commit(selected, frozen):
        assert frozen is views and len(calls)==2
        assert all(views[owner].legal(action) for owner,action in selected.items())
        runtime.ended=True
    runtime.commit_simultaneous=commit
    runtime.close=lambda: None
    runtime.terminal_evidence=lambda: dict(ended=True,truncated=False,winner=0,terminal_tick=200,
                                          reason="native-result",rejected=0,unconfirmed=0)
    monkeypatch.setattr(evaluate_policy,"NativeMatchRuntime",lambda *args,**kwargs:runtime)
    result=evaluate_policy.play_game(0,[model.policy,model.policy],[SHA,SHA],None,seed=1,candidate_owner=0,deadline=10)
    assert result["score"]==1 and result["terminal"]["ended"]
