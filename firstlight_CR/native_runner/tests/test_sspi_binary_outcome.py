"""WL protocol regressions; fixtures are not native battle evidence."""
from copy import deepcopy
from enum import IntEnum
import hashlib
import json
import math
import random

import pytest
import torch
from native_runner.training.fair_final.search import scalar_value
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi import value_experiment as experiment
from native_runner.training.sspi import value_learning as learning
from native_runner.training.sspi.collector import finalize_supervision
from native_runner.training.sspi.experiment import paired_outcomes_from_results, paired_win_rate_interval
from native_runner.training.sspi.learning import (WLLabel, terminal_wl, decisive_result,
    load_episode_training_rows, load_checkpoint, save_checkpoint, TrainingRow)
from native_runner.training.sspi.model import VALUE_ORDER, initialize_fresh_model
from native_runner.training.sspi.search import make_search_inference
from native_runner.tests.test_sspi_value_only import _value_episode, TinyValueModel
from native_runner.tests.test_sspi_temporal_augmentation import run_plain_policy, write_episode


def outcome(winner):
    return dict(winner=winner, labels=["DRAW" if winner is None else
        "WIN" if owner == winner else "LOSS" for owner in (0, 1)])


def test_binary_architecture_and_explicit_search_contract():
    model = initialize_fresh_model(20261103).eval()
    assert model.architecture["value_order"] == ["WIN", "LOSS"]
    assert model.architecture["mode_order"] == ["WAIT", "PLAY", "ABILITY"]
    assert model.value_head[-1].out_features == 2 and not hasattr(model, "wdl_head")
    assert make_search_inference(model, checkpoint_sha="a"*64,
        controller_sha="b"*64).value_order == VALUE_ORDER
    with pytest.raises(ValueError, match="differs from model architecture"):
        FrozenSearchInference(model, checkpoint_sha="a"*64, controller_sha="b"*64)
    assert scalar_value(torch.tensor([2., 0.]), value_order=VALUE_ORDER) == pytest.approx(math.tanh(1.))
    assert scalar_value(torch.zeros(3)) == 0.  # historical FAIR protocol
    for logits, order in [(torch.zeros(2), ("WIN","DRAW","LOSS")),
            (torch.zeros(2), ("LOSS","WIN"))]:
        with pytest.raises(ValueError, match="class order"):
            scalar_value(logits, value_order=order)


def test_terminal_mapping_uniform_metrics_and_old_labels():
    assert int(WLLabel.WIN) == 0 and int(WLLabel.LOSS) == 1
    assert terminal_wl(0, 0) == terminal_wl(1, 1) == WLLabel.WIN
    assert terminal_wl(0, 1) == terminal_wl(1, 0) == WLLabel.LOSS
    assert terminal_wl(0, None) is None and not decisive_result(outcome(None))
    metrics = learning.prediction_metrics([("g",0,(.5,.5)),("g",1,(.5,.5))])
    assert metrics["wl_ce"] == pytest.approx(math.log(2)) and metrics["brier"] == .5
    with pytest.raises(ValueError):
        terminal_wl(0, True)
    class OldLabel(IntEnum):
        DRAW = 1
    with pytest.raises(ValueError, match="explicit"):
        learning.ValueRow("old", None, OldLabel.DRAW)
    with pytest.raises(ValueError, match="explicit"):
        TrainingRow(None, None, OldLabel.DRAW, "DIRECT")


@pytest.mark.parametrize("label,probabilities", [(2,(.5,.5)),(True,(.5,.5)),
    (0,(.2,.2)),(0,(.3,.3,.4)),(0,(float('nan'),.5))])
def test_invalid_binary_metrics(label, probabilities):
    with pytest.raises(ValueError, match="WIN/LOSS"):
        learning.prediction_metrics([("g", label, probabilities)])


@pytest.mark.parametrize("winner", [None, 0, 1])
def test_terminal_filter_does_not_consume_randomness(winner):
    roots = [dict(owner=0, continuations=[dict(owner=0)]), dict(owner=1)]
    rows = [dict(owner=0), dict(owner=1)]
    rng, tensor_rng = random.getstate(), torch.get_rng_state().clone()
    summary = finalize_supervision(outcome(winner), roots, rows)
    assert random.getstate() == rng and torch.equal(torch.get_rng_state(), tensor_rng)
    if winner is None:
        assert roots == rows == []
        assert summary["discarded_search_roots"] == summary["discarded_value_samples"] == 2
        assert summary["discarded_continuations"] == 1
        assert summary["excluded_reason"] == "natural_draw"
    else:
        assert roots[0]["wl"] == rows[0]["wl"] == outcome(winner)["labels"][0]
        assert roots[0]["continuations"][0]["wl"] == roots[0]["wl"]


@pytest.mark.parametrize("flag", ["failed", "truncated"])
def test_failure_is_not_a_natural_draw(flag):
    roots, rows = [dict(owner=0)], [dict(owner=1)]
    with pytest.raises(ValueError, match="natural terminal"):
        finalize_supervision(dict(outcome(None), **{flag:True}), roots, rows)
    assert len(roots) == len(rows) == 1


def rewrite_episode(path, manifest, metadata):
    torch.save(manifest, path/"episode.pt")
    metadata["tensor_sha256"] = hashlib.sha256((path/"episode.pt").read_bytes()).hexdigest()
    (path/"episode.json").write_text(json.dumps(metadata))


def test_value_draw_keeps_ledger_and_excludes_all_samples(tmp_path):
    run_id, model_sha, metadata = _value_episode(tmp_path)
    manifest = torch.load(tmp_path/"episode.pt", weights_only=True)
    metadata["result"] = outcome(None)
    metadata["supervision"] = finalize_supervision(metadata["result"], manifest["roots"], manifest["value_rows"])
    metadata.update(training_admissible=False, value_samples=0)
    rewrite_episode(tmp_path, manifest, metadata)
    args = dict(expected_kind="training", run_id=run_id, model_sha=model_sha)
    assert learning.load_value_episode(tmp_path, **args)[0] == []
    assert len(json.loads((tmp_path/"actions.json").read_text())["rows"]) == 1
    manifest["value_rows"] = [dict(wl="DRAW")]
    rewrite_episode(tmp_path, manifest, metadata)
    with pytest.raises(ValueError, match="draw episode"):
        learning.load_value_episode(tmp_path, **args)


def test_search_draw_excludes_roots_and_continuations(monkeypatch, tmp_path):
    root, recorder, _, _ = run_plain_policy(monkeypatch)
    write_episode(tmp_path, root, recorder.statistics())
    manifest = torch.load(tmp_path/"episode.pt", weights_only=True)
    metadata = json.loads((tmp_path/"episode.json").read_text())
    metadata["result"] = outcome(None)
    metadata["supervision"] = finalize_supervision(metadata["result"], manifest["roots"], [])
    metadata["training_admissible"] = False
    rewrite_episode(tmp_path, manifest, metadata)
    assert load_episode_training_rows(tmp_path) == []
    assert metadata["supervision"]["discarded_continuations"] == 3


def test_reject_old_episode_schema(tmp_path):
    run_id, model_sha, metadata = _value_episode(tmp_path)
    metadata["schema"] = "sspi-value-only-episode.v1"
    (tmp_path/"episode.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="identity"):
        learning.load_value_episode(tmp_path, expected_kind="training", run_id=run_id, model_sha=model_sha)


def test_reject_old_checkpoint_schema_and_architecture_without_mutation(tmp_path):
    model = initialize_fresh_model(20261103)
    optimizer = learning.create_value_optimizer(model)
    path = tmp_path/"checkpoint.pt"
    save_checkpoint(path, model, optimizer, identity={}, optimizer_steps=0)
    original = torch.load(path, weights_only=False)
    before = semantic_digest(model.state_dict())
    for change in ("schema", "architecture"):
        envelope = deepcopy(original)
        if change == "schema":
            envelope["schema"] = "sspi-mac-pilot-checkpoint.v2"
        else:
            payload = envelope["payload"]
            payload["architecture"]["value_order"] = ["WIN", "DRAW", "LOSS"]
            payload["architecture_sha"] = semantic_digest(payload["architecture"])
            envelope["payload_sha"] = semantic_digest(payload)
        torch.save(envelope, path)
        with pytest.raises(ValueError, match="checkpoint"):
            load_checkpoint(path, model, optimizer, expected_identity={})
        assert semantic_digest(model.state_dict()) == before


def test_empty_update_preserves_state():
    model = TinyValueModel()
    optimizer = learning.create_value_optimizer(model)
    before = semantic_digest((model.state_dict(), optimizer.state_dict()))
    row = learning.ValueRow("g", torch.tensor([[1.,.2]]), WLLabel.WIN)
    for train, validation in [([], [row]), ([row], []), ([], [])]:
        result = learning.train_value_generation(model, optimizer, train, validation, seed=1)
        assert result["status"] == "skipped" and result["optimizer_steps"] == 0
        assert semantic_digest((model.state_dict(), optimizer.state_dict())) == before


def test_best_model_adamw_restore_and_failed_update_rollback(monkeypatch):
    model = TinyValueModel()
    optimizer = learning.create_value_optimizer(model)
    rows = [learning.ValueRow("g", torch.tensor([[1.,.2]]), WLLabel.WIN)]*3
    validation = [learning.ValueRow("held-out", torch.tensor([[1.,.2]]), WLLabel.WIN)]
    monkeypatch.setattr(learning, "VALUE_BATCH", 1)
    monkeypatch.setattr(learning, "VALIDATION_INTERVAL", 1)
    monkeypatch.setattr(learning, "value_predictions", lambda *_: [])
    measurements = iter([1., .8, .7, .9])
    states = []
    def metrics(_):
        states.append(semantic_digest((model.state_dict(), optimizer.state_dict())))
        return dict(wl_ce=next(measurements))
    monkeypatch.setattr(learning, "prediction_metrics", metrics)
    result = learning.train_value_generation(model, optimizer, rows, validation, seed=1)
    assert result["attempted_steps"] == 3 and result["optimizer_steps"] == 2
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == states[2]
    assert all(int(state["step"]) == 2 for state in optimizer.state.values())
    before = semantic_digest((model.state_dict(), optimizer.state_dict()))
    measurements = iter([.7, .8, .8, .8])
    with pytest.raises(RuntimeError, match="did not improve"):
        learning.train_value_generation(model, optimizer, rows, validation, seed=1)
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == before


def test_draws_do_not_add_collection_games(monkeypatch, tmp_path):
    calls = []
    def collect(_run, _config, game, kind, model, **kwargs):
        calls.append(game)
        directory = tmp_path/game
        directory.mkdir()
        metadata = dict(result=outcome(None if len(calls)%2 else 0), elapsed_seconds=1.)
        (directory/"episode.json").write_text(json.dumps(metadata))
        return directory, metadata
    monkeypatch.setattr(experiment, "_collect", collect)
    monkeypatch.setattr(experiment, "load_value_episode", lambda *a, **k: ([], {}))
    _, _, _, counts = experiment._collect_rows(tmp_path, dict(run_id="r"), "train", 64,
        "training", TinyValueModel(), actual_port=1, belief_port=2,
        belief_port_owner_1=3, transport="unused")
    assert len(calls) == 64
    assert counts == dict(collected_games=64, decisive_games=32, excluded_draw_games=32)


def test_paired_denominator_and_draw_only_pairs():
    rows = []
    for pair, winners in enumerate([(0, None), (1, 0), (None,None)], 1):
        for side, winner in enumerate(winners):
            rows.append(dict(pair_index=pair, side=side, trained_owner=side,
                game_id=f"{pair}-{side}", winner=winner,
                score=None if winner is None else float(winner==side),
                random_stream_id=f"pair-{pair}", actual_match_seed=pair,
                actual_rollout_seed=pair+10, search_roots=0, search_probability=0.))
    pairs = paired_outcomes_from_results(rows, expected_pairs=3)
    assert pairs == [(1,1),(0,2),(0,0)]
    result = paired_win_rate_interval(pairs, seed=1, replicates=100)
    assert result["mean"] == pytest.approx(1/3)
    assert result["decisive_games"] == result["excluded_draw_games"] == 3
    assert result == paired_win_rate_interval(pairs, seed=1, replicates=100)
    empty = paired_win_rate_interval([(0,0)], seed=1)
    assert empty["status"] == "undefined" and empty["mean"] is empty["low"] is empty["high"] is None
    rows[1]["score"] = .5
    with pytest.raises(ValueError, match="score"):
        paired_outcomes_from_results(rows, expected_pairs=3)


@pytest.mark.parametrize("terminal", [None, 0.])
def test_binary_puct_completes_128_and_preserves_neutral_legacy_visits(terminal):
    from native_runner.tests.test_fair_final_search import search_fixture
    results = []
    for order in [("WIN","DRAW","LOSS"), VALUE_ORDER]:
        search, info, arguments = search_fixture(terminal=terminal)
        old_predict = search.inference.predict
        search.inference.value_order = order
        def predict(state, fn=old_predict, width=len(order)):
            policy, _ = fn(state)
            return policy, torch.zeros(1, width)
        search.inference.predict = predict
        result = search.run(info, **arguments)
        assert result.result["valid_simulations"] == 128
        assert sum(count for _, count in result.policy_target) == 128
        assert search.belief.world.closed
        assert search.belief.world.current.sha256 == info.sha256
        results.append(result)
    assert results[0].policy_target == results[1].policy_target
    assert results[0].selected == results[1].selected
    assert results[0].result["candidates"] == results[1].result["candidates"]


@pytest.mark.parametrize("empty_kind", ["training", "validation"])
def test_orchestrator_skips_empty_generation_after_fixed_64_and_16(monkeypatch, tmp_path, empty_kind):
    model = TinyValueModel()
    optimizer = learning.create_value_optimizer(model)
    monkeypatch.setattr(experiment, "_checkpoint_model", lambda *a, **k:
        (model, optimizer, dict(model_sha="parent", file_sha="checkpoint")))
    calls = []
    row = learning.ValueRow("g", torch.tensor([[1.,.2]]), WLLabel.WIN)
    def collect(_run, _config, prefix, count, kind, model, **kwargs):
        calls.append((kind, count))
        empty = kind == empty_kind
        return ([] if empty else [row], {}, [1.]*count,
            dict(collected_games=count, decisive_games=0 if empty else count,
                excluded_draw_games=count if empty else 0))
    monkeypatch.setattr(experiment, "_collect_rows", collect)
    record = experiment._run_generation(tmp_path, dict(run_id="r", checkpoint_identity={}), 1,
        bundle=None, actual_port=1, belief_port=2, belief_port_owner_1=3, transport="unused")
    assert calls == [("training",64),("validation",16)]
    assert record["status"] == "skipped" and record["optimizer_steps"] == 0
    assert not (tmp_path/"checkpoints/generation-0001.pt").exists()


def test_old_run_cli_rejection_preserves_status(monkeypatch, tmp_path):
    import sys
    (tmp_path/"run_config.json").write_text(json.dumps(dict(schema="sspi-value-only-run.v1")))
    old_status = '{"status":"complete","generation":1}'
    (tmp_path/"status.json").write_text(old_status)
    monkeypatch.setattr(sys, "argv", ["value_experiment", "--run-dir", str(tmp_path), "--run-id", "new"])
    with pytest.raises(ValueError, match="preserve"):
        experiment.main()
    assert (tmp_path/"status.json").read_text() == old_status
    assert not (tmp_path/"orchestrator.lock").exists()


def test_old_search_run_cli_rejection_preserves_status(monkeypatch, tmp_path):
    import sys
    from native_runner.training.sspi import experiment as search_experiment
    (tmp_path/"run_config.json").write_text(json.dumps(dict(schema="sspi-mac-pilot-run.v1")))
    old_status = '{"status":"complete","generation":1}'
    (tmp_path/"postgame_status.json").write_text(old_status)
    monkeypatch.setattr(sys, "argv", ["experiment", "--run-dir", str(tmp_path),
        "--evaluate-generation", "1"])
    with pytest.raises(ValueError, match="preserve"):
        search_experiment.main()
    assert (tmp_path/"postgame_status.json").read_text() == old_status
