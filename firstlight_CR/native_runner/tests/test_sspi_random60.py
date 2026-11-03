"""Preregistered schedule and recovery tests; fixtures are not native evidence."""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from types import SimpleNamespace

import pytest
import torch

from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi import experiment as exp
from native_runner.training.sspi import learning
from native_runner.training.sspi import random60 as run
from native_runner.training.sspi.collector import episode_seed_identity
from native_runner.training.sspi.model import initialize_fresh_model


@pytest.mark.parametrize("count", [1, 127, 128, 129, 369])
def test_all_roots_two_epochs_with_real_tail_and_local_rng(count):
    import random
    before = random.getstate()
    batches = run.all_root_batches(count, seed=7)
    assert random.getstate() == before
    assert batches == run.all_root_batches(count, seed=7)
    assert Counter(i for batch in batches for i in batch["indices"]) == {i:2 for i in range(count)}
    for epoch in (1, 2):
        ids = [i for batch in batches if batch["epoch"] == epoch for i in batch["indices"]]
        assert sorted(ids) == list(range(count))
    assert all(0 < len(batch["indices"]) <= 128 for batch in batches)


@pytest.mark.parametrize("count", [1, 5, 127, 128])
def test_short_batch_uses_actual_root_weight_including_tail(monkeypatch, count):
    model = torch.nn.Linear(1, 1, bias=False)
    with torch.no_grad():
        model.weight.zero_()
    reference = deepcopy(model)
    opt = learning.create_optimizer(model, lr=1e-5)
    reference_opt = learning.create_optimizer(reference, lr=1e-5)
    rows = [SimpleNamespace(search_mode="SEARCH", target=float(i+1)) for i in range(count)]
    def losses(model, rows):
        target = model.weight.new_tensor([r.target for r in rows])
        loss = (model.weight.flatten()[0]-target).square().mean()
        return dict(loss=2*loss, policy_loss=loss, value_loss=loss,
            root_mode_policy_loss=loss, continuation_loss=loss*0, continuation_rows=0)
    monkeypatch.setattr(learning, "search_root_loss", losses)
    result = learning.train_generation_step(model, opt, rows, validation_rows=rows, allow_partial=True)
    losses(reference, rows)["loss"].backward()
    norm = torch.nn.utils.clip_grad_norm_(reference.parameters(), 1.)
    reference_opt.step()
    assert result["gradient_norm"] == pytest.approx(float(norm), rel=1e-5)
    assert result["loss"] == pytest.approx(2*sum(r.target**2 for r in rows)/count, rel=1e-6)
    assert torch.allclose(model.weight, reference.weight, atol=1e-10)
    assert result["search_rows"] == count and result["accumulation_steps"] == math.ceil(count/4)


def test_partial_is_explicit_and_direct_rows_are_rejected():
    model = torch.nn.Linear(1, 1)
    opt = learning.create_optimizer(model)
    with pytest.raises(ValueError, match="128 SEARCH"):
        learning.train_generation_step(model, opt, [SimpleNamespace(search_mode="SEARCH")])
    with pytest.raises(ValueError, match="128 SEARCH"):
        learning.train_generation_step(model, opt, [SimpleNamespace(search_mode="DIRECT")], allow_partial=True)


def test_joint_rejection_preserves_prior_steps_and_resume_never_reexposes(monkeypatch, tmp_path):
    torch.set_num_threads(1)
    seed = 20261103
    model = initialize_fresh_model(seed)
    optimizer = learning.create_optimizer(model, lr=1e-5)
    calls = []
    def step(model, optimizer, rows, **kwargs):
        calls.append(len(rows))
        if len(calls) == 2:
            raise learning.UpdateRejected("no non-regressing step")
        next(model.parameters()).flatten()[0].backward()
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        return dict(optimizer_steps=1, gradient_norm=1.)
    monkeypatch.setattr(run, "train_generation_step", step)
    config = dict(seed=seed, run_id="fixture")
    args = dict(input_steps=0, data_identity={})
    result = run.train_all_roots(tmp_path, config, 1, model, optimizer, [object()]*129, [object()], **args)
    assert calls == [128, 1] and result["optimizer_steps"] == 1
    assert result["stopped_on_rejection"]
    assert sum(result["attempted_counts"]) == 129 and sum(result["accepted_counts"]) == 128
    state_sha = semantic_digest((model.state_dict(), optimizer.state_dict()))
    fresh = initialize_fresh_model(seed)
    fresh_opt = learning.create_optimizer(fresh, lr=1e-5)
    repeated = run.train_all_roots(tmp_path, config, 1, fresh, fresh_opt, [object()]*129, [object()], **args)
    assert repeated == result and calls == [128, 1]
    assert semantic_digest((fresh.state_dict(), fresh_opt.state_dict())) == state_sha


def test_uncommitted_batch_intent_stops_without_repeating(tmp_path):
    model = initialize_fresh_model(20261103)
    opt = learning.create_optimizer(model, lr=1e-5)
    path = tmp_path / "joint/generation-0001"
    path.mkdir(parents=True)
    (path / "attempt-0001.json").write_text("{}")
    before = semantic_digest((model.state_dict(), opt.state_dict()))
    with pytest.raises(RuntimeError, match="uncommitted"):
        run.train_all_roots(tmp_path, dict(seed=1, run_id="fixture"), 1,
            model, opt, [object()], [object()], input_steps=0, data_identity={})
    assert semantic_digest((model.state_dict(), opt.state_dict())) == before


def test_sequential_evidence_minimum_draws_null_and_numeric_range():
    assert not exp.paired_sequential_evidence([(2,2)]*63)["passed"]
    assert exp.paired_sequential_evidence([(2,2)]*64)["passed"]
    assert not exp.paired_sequential_evidence([(0,0)]*1500)["passed"]
    assert exp.paired_sequential_evidence([(0,0)]*1500)["e_value"] == 1.
    assert exp.paired_sequential_evidence([(0,2)]*1500)["log_e_value"] < 0
    assert math.isfinite(exp.paired_sequential_evidence([(2,2)]*1500)["log_e_value"])
    # A complete correlated pair is either two wins (.6) or two losses (.4).
    for lam in (.05,.1,.2,.4,.8):
        assert .6*(1+lam*.8) + .4*(1-lam*1.2) == pytest.approx(1.)
    with pytest.raises(ValueError):
        exp.paired_sequential_evidence([(1, 0)])


def test_selection_uses_predefined_thresholds_and_tie_earliest():
    def result(g, rate, count=128):
        return dict(generation=g, score=dict(mean=rate, decisive_games=count))
    screens = [result(1,.61), result(2,.64), result(4,.64)]
    assert run.choose_candidate(screens, last_generation=4) is None
    assert run.choose_candidate(screens, last_generation=8)["generation"] == 2
    assert run.choose_candidate([result(1,.99,63)], last_generation=8) is None
    assert run.choose_candidate([result(1,.66), result(2,.9)], last_generation=2)["generation"] == 1


def _evaluation_fixture(monkeypatch, tmp_path):
    config = dict(run_id="new-fixture", seed=20261103, arm="A_5PCT_128", deck=list(HOG_DECK),
        engine_sha256="engine", collector_source_sha="source")
    calls = []
    def collect(**kwargs):
        calls.append(kwargs["game_id"])
        game_id, stream = kwargs["game_id"], kwargs["random_stream_id"]
        side = (int(game_id.rsplit("-",1)[1])-1)%2
        directory = tmp_path / "episodes" / game_id
        directory.mkdir(parents=True)
        (directory / "episode.pt").write_bytes(b"fixture tensor")
        (directory / "actions.json").write_bytes(b"fixture actions")
        metadata = dict(schema="sspi-mac-pilot-episode.v2", status="complete", episode_kind="evaluation",
            training_admissible=False, search_probability=0., search_roots=0,
            tensor_sha256=run.sha(directory/"episode.pt"), actions_sha256=run.sha(directory/"actions.json"),
            identity=dict(run_id=config["run_id"], arm=config["arm"], seed=config["seed"], device="cpu",
                game_id=game_id, model_sha256_by_owner={str(side):"trained",str(1-side):"theta0"},
                engine_sha256="engine", source_sha256="source",
                **episode_seed_identity(config["seed"],run_id=config["run_id"],arm=config["arm"],
                    game_id=game_id,random_stream_id=stream)),
            result=dict(winner=side,labels=["WIN" if owner==side else "LOSS" for owner in (0,1)]))
        (directory/"episode.json").write_text(json.dumps(metadata))
        return directory, metadata
    monkeypatch.setattr(exp,"_collect_one",collect)
    args=dict(run_config=config,run_dir=tmp_path,generation=1,trained_model=object(),trained_model_sha="trained",
        theta0_model=object(),theta0_sha="theta0",checkpoint_sha="checkpoint",actual_port=1,belief_port=2,
        transport="event-json-delta",evaluation_pairs=100,output_name="formal.json",evaluation_domain="formal",
        sequential=True)
    return args,calls


def test_formal_stops_at_complete_pairs_and_resumes_without_duplicates(monkeypatch,tmp_path):
    args,calls=_evaluation_fixture(monkeypatch,tmp_path)
    record=exp._run_paired_evaluation(**args)
    assert len(calls)==128 and record["sequential_evidence"]["passed"]
    assert record["protocol"]["paired_seeds"]==64
    assert exp._run_paired_evaluation(**args)==record and len(calls)==128
    with pytest.raises(ValueError,match="identity changed"):
        exp._run_paired_evaluation(**dict(args,checkpoint_sha="changed"))
    with pytest.raises(ValueError,match="identity changed"):
        exp._run_paired_evaluation(**dict(args,evaluation_pairs=101))


def test_eval_half_pair_pauses_and_domains_never_share_seed(monkeypatch,tmp_path):
    args,calls=_evaluation_fixture(monkeypatch,tmp_path)
    broken=tmp_path/"episodes/E-formal-g0001-game-0002"
    broken.mkdir(parents=True)
    (broken/"quarantine.json").write_text("{}")
    with pytest.raises(RuntimeError,match="inspection"):
        exp._run_paired_evaluation(**args)
    assert calls==["E-formal-g0001-game-0001"]
    for domain in ("screening","formal"):
        seed=episode_seed_identity(20261103,run_id="fixture",arm="A_5PCT_128",game_id="unused",
            random_stream_id=f"sspi-{domain}-g0001-pair-0001")
        if domain=="screening": first=seed
        else: assert seed["actual_match_seed"]!=first["actual_match_seed"]


def test_fixed_protocol_matches_registered_design():
    from pathlib import Path
    root=Path(__file__).resolve().parents[3]
    design=json.loads((root/"docs/SSPI_RANDOM_TO_60_PLAN_2026-11-03.json").read_text())
    assert run.PROTOCOL["joint_lr"]==design["joint_update"]["optimizer"]["lr"]
    assert run.PROTOCOL["maximum_epochs"]==design["joint_update"]["maximum_epochs"]
    assert run.PROTOCOL["seed"]==design["initialization_seed"]
    assert run.PROTOCOL["direct_value_stage"]["train_games"]==design["direct_value_stage"]["train_games"]


def test_real_model_short_batch_binary_update_and_roundtrip(tmp_path):
    from native_runner.tests.test_sspi_search_performance import _real_information
    from native_runner.training.sspi.protocol import SSPIAction
    torch.set_num_threads(1)
    model = initialize_fresh_model(20261103)
    optimizer = learning.create_optimizer(model, lr=1e-5)
    info = _real_information()
    row = learning.TrainingRow(info, info.legality, learning.WLLabel.WIN, "SEARCH",
        ((SSPIAction.wait(), 128),), 128)
    before = semantic_digest(model.state_dict())
    update = learning.train_generation_step(model, optimizer, [row]*5,
        validation_rows=[row], allow_partial=True)
    assert update["gradient_norm"] > 0 and update["optimizer_steps"] == 1
    assert before != semantic_digest(model.state_dict())
    probe = run.mode_probe(model, [row])
    assert len(probe["states"]) == 1 and run.mode_drift(probe, probe)["value"] == 0.
    checkpoint = learning.save_checkpoint(tmp_path/"short.pt", model, optimizer,
        identity=dict(protocol="random60-short-batch-test"), optimizer_steps=1)
    fresh = initialize_fresh_model(20261103)
    fresh_opt = learning.create_optimizer(fresh, lr=1e-5)
    restored = learning.load_checkpoint(checkpoint["path"], fresh, fresh_opt,
        expected_identity=dict(protocol="random60-short-batch-test"))
    assert restored["model_sha"] == checkpoint["model_sha"]
    assert semantic_digest(fresh_opt.state_dict()) == semantic_digest(optimizer.state_dict())


def test_generation_stages_checkpoint_commit_and_resume(monkeypatch, tmp_path):
    torch.set_num_threads(1)
    model = initialize_fresh_model(20261103)
    optimizer = learning.create_optimizer(model, lr=1e-5)
    config = dict(seed=20261103, run_id="stages", engine_sha256="engine", transport="event-json-delta",
        ports=dict(actual=1,belief_by_owner={"0":2,"1":3}), initialization=dict(model_sha="theta0"))
    calls = []
    def collect(directory, config, generation, model, *, kind, count):
        calls.append((kind,count))
        return [object()], dict(files={},outcomes=dict(collected_games=count,decisive_games=count,excluded_draw_games=0))
    def joint(*args, **kwargs):
        calls.append("joint")
        next(model.parameters()).flatten()[0].backward()
        optimizer.step(); optimizer.zero_grad(set_to_none=True)
        return dict(optimizer_steps=1)
    def direct(**kwargs):
        calls.append("direct")
        assert kwargs["optimizer_steps"]==1
        return dict(status="rolled_back",optimizer_steps=0)
    monkeypatch.setattr(run,"collect_split",collect)
    monkeypatch.setattr(run,"train_all_roots",joint)
    monkeypatch.setattr(run,"run_direct_value_stage",direct)
    monkeypatch.setattr(run,"mode_probe",lambda *args: dict(states=[dict(fair_sha="fixed",probabilities=[.5,.5,0.])]))
    record,steps=run.run_generation(tmp_path,config,1,model,optimizer,0)
    assert calls==[("training",4),("validation",2),"joint","direct"] and steps==1
    repeated,steps=run.run_generation(tmp_path,config,1,model,optimizer,0)
    assert repeated==record and len(calls)==4
    # A crash between checkpoint commit and JSON publication must not repeat the update.
    (tmp_path/"generation-0001.json").unlink()
    repeated,steps=run.run_generation(tmp_path,config,1,model,optimizer,0)
    assert repeated==record and len(calls)==4
