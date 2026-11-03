"""Head-only post-SSPI regressions. Synthetic episodes are not native run evidence."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_sspi_search_performance import _real_information
from native_runner.tests.test_sspi_value_only import TinyValueModel
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi import value_calibration as stage
from native_runner.training.sspi import value_learning as learning
from native_runner.training.sspi.augmentation import serialize_information
from native_runner.training.sspi.collector import VALUE_EPISODE_SCHEMA, value_sample_gate, finalize_supervision
from native_runner.training.sspi.learning import WLLabel, create_optimizer
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.protocol import SSPIAction


def test_head_only_update_freezes_whole_policy_and_continues_value_moments():
    torch.manual_seed(11)
    model = TinyValueModel().eval()
    original_optimizer = create_optimizer(model)
    model.value_only(torch.tensor([[1., .25]])).sum().backward()
    original_optimizer.step()
    original_optimizer.zero_grad(set_to_none=True)
    actor_state = semantic_digest({id(p): s for p, s in original_optimizer.state.items()
        if all(p is not v for v in model.value_head.parameters())})
    frozen = learning.policy_function_sha(model)
    probe_before = model.policy_head(model.shared(torch.tensor([[1., .25]])).tanh()).detach()
    optimizer = learning.create_value_optimizer(model, head_only=True)
    stage._copy_value_optimizer_state(model, original_optimizer, optimizer)
    rows = [learning.ValueRow("train", torch.tensor([[1., .25]]), WLLabel.WIN)]*129
    held = [learning.ValueRow("held", torch.tensor([[1., .25]]), WLLabel.WIN)]
    result = learning.train_value_generation(model, optimizer, rows, held, seed=7, head_only=True)
    assert result["status"] == "updated" and result["attempted_steps"] == 2
    assert learning.policy_function_sha(model) == frozen
    assert torch.equal(probe_before, model.policy_head(model.shared(torch.tensor([[1., .25]])).tanh()))
    assert all(p.grad is None for name, p in model.named_parameters() if not name.startswith("value_head."))
    stage._copy_value_optimizer_state(model, optimizer, original_optimizer)
    assert all(int(s["step"]) == 3 for s in optimizer.state.values())
    assert actor_state == semantic_digest({id(p): s for p, s in original_optimizer.state.items()
        if all(p is not v for v in model.value_head.parameters())})


def test_head_only_best_model_optimizer_and_no_improvement_rollback(monkeypatch):
    model = TinyValueModel()
    optimizer = learning.create_value_optimizer(model, head_only=True)
    train = [learning.ValueRow("train", torch.tensor([[1., .2]]), WLLabel.WIN)]*3
    held = [learning.ValueRow("held", torch.tensor([[1., .2]]), WLLabel.WIN)]
    monkeypatch.setattr(learning, "VALUE_BATCH", 1)
    monkeypatch.setattr(learning, "VALIDATION_INTERVAL", 1)
    monkeypatch.setattr(learning, "value_predictions", lambda *_: [])
    measurements, states = iter([1., .8, .7, .9]), []
    def metrics(_):
        states.append(semantic_digest((model.state_dict(), optimizer.state_dict())))
        return dict(wl_ce=next(measurements))
    monkeypatch.setattr(learning, "prediction_metrics", metrics)
    result = learning.train_value_generation(model, optimizer, train, held, seed=1, head_only=True)
    assert result["optimizer_steps"] == 2 and result["attempted_steps"] == 3
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == states[2]
    before = semantic_digest((model.state_dict(), optimizer.state_dict()))
    measurements = iter([.7, .8, .8, .8])
    result = learning.train_value_generation(model, optimizer, train, held, seed=1, head_only=True)
    assert result["status"] == "rolled_back" and result["optimizer_steps"] == 0
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == before


def test_scope_and_episode_overlap_rejected():
    model = TinyValueModel()
    train = [learning.ValueRow("train", torch.tensor([[1., .2]]), WLLabel.WIN)]
    held = [learning.ValueRow("held", torch.tensor([[1., .2]]), WLLabel.WIN)]
    optimizer = learning.create_value_optimizer(model)
    with pytest.raises(ValueError, match="scope"):
        learning.train_value_generation(model, optimizer, train, held, seed=1, head_only=True)
    with pytest.raises(ValueError, match="overlap"):
        learning.train_value_generation(model, optimizer, train, train, seed=1)


@pytest.mark.parametrize("device", ["cpu", "mps"])
def test_real_model_head_update_preserves_all_legal_policy_probabilities(device):
    if device == "mps" and not torch.backends.mps.is_available():
        pytest.skip("MPS is unavailable")
    torch.set_num_threads(1)
    model = initialize_fresh_model(20261103, device=device).eval()
    information = _real_information()
    def probabilities():
        with torch.inference_mode():
            policy = model(information, [information.legality])[0][0]
            actions = [SSPIAction.wait()] + [SSPIAction.play(information.legality.card_ids[c], s, d)
                for c, s, d in information.legality.plays.nonzero().tolist()]
            return torch.stack([policy.log_prob(action) for action in actions])
    before, frozen = probabilities(), learning.policy_function_sha(model)
    value_before = semantic_digest(model.value_head.state_dict())
    optimizer = learning.create_value_optimizer(model, head_only=True)
    learning.value_loss(model, [learning.ValueRow("train", information, WLLabel.WIN)]).backward()
    assert all(p.grad is None for name, p in model.named_parameters() if not name.startswith("value_head."))
    assert any(p.grad is not None and bool(p.grad.abs().sum() > 0) for p in model.value_head.parameters())
    torch.nn.utils.clip_grad_norm_(model.value_head.parameters(), 1.)
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    after = probabilities()
    assert learning.policy_function_sha(model) == frozen
    if device == "cpu":
        assert torch.equal(before, after)
    else:
        # MPS cache rebuild/kernel rounding can differ at the float32 last bit.
        torch.testing.assert_close(before, after, rtol=0., atol=1e-6)
    assert semantic_digest(model.value_head.state_dict()) != value_before


def _fake_collector(calls, *, draw_training=False):
    def collect(**kwargs):
        calls.append(kwargs)
        assert kwargs["search_probability"] == 0. and kwargs["value_sample_probability"] == .5
        assert kwargs["temporal_augmentation"] is False
        assert all(not p.requires_grad for p in kwargs["model"].parameters())
        assert not kwargs["model"].training
        output = kwargs["output_dir"]
        output.mkdir(parents=True)
        info = _real_information()
        run_id, game_id, seed = kwargs["run_id"], kwargs["game_id"], kwargs["seed"]
        index = next(i for i in range(100) if value_sample_gate(seed,
            run_id=run_id, game_id=game_id, owner=0, decision_index=i, probability=.5)[0])
        _, gate = value_sample_gate(seed, run_id=run_id, game_id=game_id, owner=0,
            decision_index=index, probability=.5)
        model_sha = semantic_digest(kwargs["model"].state_dict())
        identity = dict(run_id=run_id, game_id=game_id, seed=seed, arm=kwargs["arm"],
            random_stream_id=game_id, collection_protocol="value-only.v2",
            value_sample_probability=.5, source_sha256=stage._source_sha(),
            engine_sha256="synthetic-engine", native_transport=kwargs["transport"], device="cpu",
            actual_env_id=kwargs["actual_env_id"], native_ports=dict(actual=kwargs["actual_port"]),
            model_sha256_by_owner={"0":model_sha,"1":model_sha})
        rows = [dict(**serialize_information(info), decision_index=index, value_gate_seed=gate,
            action="wait", executed_action="wait")]
        draw = draw_training and kwargs["episode_kind"] == "training"
        result = dict(winner=None if draw else 0, labels=["DRAW","DRAW"] if draw else ["WIN","LOSS"])
        supervision = finalize_supervision(result, [], rows)
        torch.save(dict(schema=VALUE_EPISODE_SCHEMA, identity=identity, roots=[], value_rows=rows), output/"episode.pt")
        action = dict(owner=0, tick=info.tick, decision_index=index, fair_sha256=info.sha256,
            mode="DIRECT", value_selected=True, value_gate_seed=gate, action="wait", executed_action="wait")
        (output/"actions.json").write_text(json.dumps(dict(rows=[action])))
        metadata = dict(schema=VALUE_EPISODE_SCHEMA, identity=identity, status="complete",
            episode_kind=kwargs["episode_kind"], training_admissible=kwargs["episode_kind"]=="training" and not draw,
            search_roots=0, search_probability=0., value_sample_probability=.5,
            value_samples=len(rows), decisions=dict(decisions={"0":1,"1":0}, value_samples={"0":1,"1":0}),
            result=result, supervision=supervision, elapsed_seconds=1.,
            tensor_sha256=stage._sha(output/"episode.pt"), actions_sha256=stage._sha(output/"actions.json"))
        (output/"episode.json").write_text(json.dumps(metadata))
        return metadata
    return collect


def _stage_args(tmp_path):
    model = initialize_fresh_model(20261103)
    optimizer = create_optimizer(model)
    model.value_head[-1].bias.square().sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    return dict(run_dir=tmp_path, config=dict(run_id="stage-fixture", seed=20261103, device="cpu",
        direct_value_stage=dict(schema=stage.STAGE_SCHEMA, train_games=2, validation_games=1)),
        generation=1, model=model, optimizer=optimizer, optimizer_steps=1,
        engine_sha="synthetic-engine", actual_port=26789, belief_port=26790, transport="event-json-delta")


def test_stage_collects_new_policy_then_restores_without_repeating_exposure(monkeypatch, tmp_path):
    torch.set_num_threads(1)
    args, calls = _stage_args(tmp_path), []
    base_model, base_optimizer = deepcopy(args["model"].state_dict()), deepcopy(args["optimizer"].state_dict())
    monkeypatch.setattr(stage, "collect_episode", _fake_collector(calls))
    result = stage.run_direct_value_stage(**args)
    assert len(calls) == 3 and result["search_roots"] == 0
    assert result["status"] == "updated" and result["optimizer_steps"] == 1
    assert result["policy_unchanged"] and result["checkpoint_roundtrip"]
    assert result["validation_metrics"]["wl_ce"] < result["update"]["validation_before"]["wl_ce"]
    final_sha = semantic_digest((args["model"].state_dict(), args["optimizer"].state_dict()))
    args["model"].load_state_dict(base_model)
    args["optimizer"].load_state_dict(base_optimizer)
    monkeypatch.setattr(stage, "collect_episode", lambda **_: pytest.fail("completed stage recollected"))
    assert stage.run_direct_value_stage(**args) == result
    assert semantic_digest((args["model"].state_dict(), args["optimizer"].state_dict())) == final_sha
    args["model"].load_state_dict(base_model)
    args["optimizer"].load_state_dict(base_optimizer)
    (tmp_path/"direct-value/generation-0001/episodes/training-0001/actions.json").write_text("{}")
    with pytest.raises(ValueError, match="evidence checksum"):
        stage.run_direct_value_stage(**args)
    assert semantic_digest(args["model"].state_dict()) == semantic_digest(base_model)


def test_all_draw_training_skips_update_without_extra_collection(monkeypatch, tmp_path):
    args, calls = _stage_args(tmp_path), []
    before = semantic_digest((args["model"].state_dict(), args["optimizer"].state_dict()))
    monkeypatch.setattr(stage, "collect_episode", _fake_collector(calls, draw_training=True))
    result = stage.run_direct_value_stage(**args)
    assert result["status"] == "skipped" and result["optimizer_steps"] == 0
    assert len(calls) == 3 and result["training"]["outcomes"]["excluded_draw_games"] == 2
    assert semantic_digest((args["model"].state_dict(), args["optimizer"].state_dict())) == before


def test_legacy_disabled_and_invalid_stage_plans(monkeypatch, tmp_path):
    monkeypatch.setattr(stage, "collect_episode", lambda **_: pytest.fail("legacy run collected"))
    args = _stage_args(tmp_path)
    args["config"].pop("direct_value_stage")
    assert stage.run_direct_value_stage(**args) is None
    assert not (tmp_path/"direct-value").exists()
    for count in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="positive"):
            stage.stage_config(dict(direct_value_stage=dict(schema=stage.STAGE_SCHEMA,
                train_games=count, validation_games=1)))


def test_first_and_next_generation_use_calibrated_checkpoint(monkeypatch, tmp_path):
    from native_runner.training.sspi import experiment
    from native_runner.training.sspi.learning import save_checkpoint
    torch.set_num_threads(1)
    seed = 20261103
    model = initialize_fresh_model(seed)
    optimizer = create_optimizer(model)
    initial = save_checkpoint(tmp_path/"initial.pt", model, optimizer,
        identity=dict(seed=seed), optimizer_steps=0)
    config = dict(schema="sspi-mac-pilot-run.v2", status="collecting", run_id="two-phase-fixture",
        arm="A_5PCT_128", seed=seed, device="cpu", transport="event-json-delta", initialization=initial,
        direct_value_stage=dict(schema=stage.STAGE_SCHEMA, train_games=2, validation_games=1))
    (tmp_path/"run_config.json").write_text(json.dumps(config))
    search_calls, direct_calls, evaluation_models = [], [], []
    def search_collect(**kwargs):
        item = kwargs["model"]
        directory = tmp_path/"episodes"/kwargs["game_id"]
        directory.mkdir(parents=True)
        kind = kwargs["episode_kind"]
        sha = semantic_digest(item.state_dict())
        metadata = dict(schema="sspi-mac-pilot-episode.v2", status="complete", episode_kind=kind,
            training_admissible=kind=="training", result=dict(winner=0,labels=["WIN","LOSS"]),
            search_probability=.05, identity=dict(run_id=config["run_id"], arm=config["arm"],
                seed=seed, device="cpu", engine_sha256="synthetic-engine", source_sha256="fixture",
                model_sha256_by_owner={"0":sha,"1":sha}))
        (directory/"episode.json").write_text(json.dumps(metadata))
        search_calls.append((kwargs["game_id"], sha))
        return directory, metadata
    first_dir, first = search_collect(model=model, game_id="A-game-0001", episode_kind="training")
    monkeypatch.setattr(experiment, "wait_for_episode_process", lambda *a, **k: first)
    monkeypatch.setattr(experiment, "_collect_one", search_collect)
    monkeypatch.setattr(experiment, "production_semantic_bundle", lambda: None)
    monkeypatch.setattr(experiment, "load_episode_training_rows", lambda _: [SimpleNamespace(continuations=())]*128)
    monkeypatch.setattr(experiment, "load_episode_validation_rows", lambda _: [SimpleNamespace(continuations=())])
    def search_update(model, optimizer, *args, **kwargs):
        next(model.parameters()).sum().backward()
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        return dict(optimizer_steps=1)
    monkeypatch.setattr(experiment, "train_generation_step", search_update)
    monkeypatch.setattr(experiment, "search_root_loss", lambda *a: dict(loss=torch.tensor(1.)))
    monkeypatch.setattr(stage, "collect_episode", _fake_collector(direct_calls))
    def evaluate(**kwargs):
        sha = semantic_digest(kwargs["trained_model"].state_dict())
        assert sha == kwargs["trained_model_sha"]
        evaluation_models.append(sha)
        return dict(score=dict(mean=.5), protocol=dict(games=2))
    monkeypatch.setattr(experiment, "_run_paired_evaluation", evaluate)
    first_record = experiment.run_postgame(run_dir=tmp_path, first_episode_dir=first_dir,
        wait_pid=123, train_games=1, validation_games=1, evaluation_pairs=1)["training"]
    second = experiment.run_next_generation(run_dir=tmp_path, parent_generation=1,
        train_games=1, validation_games=1, evaluation_pairs=1)
    for record, evaluated in zip((first_record, second), evaluation_models):
        assert record["direct_value_stage"]["status"] == "updated"
        assert record["direct_value_stage"]["output_model_sha"] == record["checkpoint"]["model_sha"] == evaluated
        assert record["checkpoint_restore"]["optimizer_steps"] == record["generation"]*2
    assert second["parent_model_sha256"] == first_record["checkpoint"]["model_sha"]
    assert all(sha==first_record["checkpoint"]["model_sha"] for _,sha in search_calls[2:])
    assert len(direct_calls) == 6
    config.pop("direct_value_stage")
    (tmp_path/"run_config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="historical run"):
        experiment.run_next_generation(run_dir=tmp_path, parent_generation=2,
            train_games=1, validation_games=1, evaluation_pairs=1)
