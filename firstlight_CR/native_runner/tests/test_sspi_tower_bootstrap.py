"""Synthetic engineering checks; not evidence of native or held-out learning success."""

from copy import deepcopy
from dataclasses import replace
import json
import random
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_sspi_search_performance import _real_information
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi import tower_bootstrap as warmup
from native_runner.training.sspi import tower_experiment as experiment
from native_runner.training.sspi.augmentation import serialize_information
from native_runner.training.sspi.collector import finalize_supervision, value_sample_gate
from native_runner.training.sspi.learning import save_checkpoint, load_checkpoint
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.value_learning import ValueRow, policy_function_sha
from native_runner.training.sspi.learning import WLLabel


@pytest.fixture(autouse=True)
def one_thread():
    torch.set_num_threads(1)


def towers(own=3000., enemy=2000.):
    return [dict(owner=side, tower_kind=kind, hitpoints=own if side == 0 else enemy,
        max_hitpoints=5000., active=True) for side in (0, 1)
        for kind in ("king", "princess_left", "princess_right")]


def test_formula_example_equal_swap_and_monotonic():
    target = warmup.tower_target(towers(), 0)
    assert target["score"] == .2 and target["soft_target"] == [.6, .4]
    assert target["value_order"] == ["WIN", "LOSS"]
    assert warmup.tower_target(towers(2000., 2000.), 0)["score"] == 0.
    reverse = warmup.tower_target(towers(), 1)
    assert reverse["score"] == -.2 and reverse["soft_target"] == [.4, .6]
    assert warmup.tower_target(towers(2999.), 0)["score"] < .2
    assert sum(target["soft_target"]) == 1.


@pytest.mark.parametrize("mutation", [
    lambda t: t.pop(), lambda t: t[0].pop("hitpoints"),
    lambda t: t[0].update(hitpoints=float("nan")),
    lambda t: t[0].update(hitpoints=-1), lambda t: t[0].update(hitpoints=6000),
    lambda t: t[0].update(max_hitpoints=0), lambda t: t.append(t[0]),
    lambda t: [x.update(hitpoints=0., active=False) for x in t],
    lambda t: t[0].update(hitpoints=0., active=True)])
def test_invalid_public_towers_are_rejected(mutation):
    values = towers()
    mutation(values)
    with pytest.raises(ValueError):
        warmup.tower_target(values, 0)


def test_padding_ignored_destroyed_is_explicit_zero_and_missing_is_not_zero():
    values = towers()
    values.append(dict(mask=False, hitpoints=float("nan")))
    assert warmup.tower_target(values, 0)["self_hp"] == 9000.
    values[0].update(hitpoints=0., active=False)
    assert warmup.tower_target(values, 0)["self_hp"] == 6000.
    values[0]["mask"] = False
    with pytest.raises(ValueError, match="missing"):
        warmup.tower_target(values, 0)


def test_capture_and_formula_revalidation_leave_real_wl_untouched():
    info = _real_information()
    observation = SimpleNamespace(owner=info.owner, tick=info.tick,
        towers=[SimpleNamespace(**t) for t in towers()])
    raw = serialize_information(info)
    target = warmup.capture_tower_target(observation, info)
    assert warmup.validate_target(target, raw)
    corrupted = deepcopy(target)
    corrupted["soft_target"] = [1., 0.]
    with pytest.raises(ValueError, match="formula"):
        warmup.validate_target(corrupted, raw)
    rows = [dict(owner=0, tower_hp_target=target)]
    finalize_supervision(dict(winner=1, labels=["LOSS", "WIN"]), [], rows)
    assert rows[0]["wl"] == "LOSS" and rows[0]["tower_hp_target"]["soft_target"] == [.6, .4]
    finalize_supervision(dict(winner=None, labels=["DRAW", "DRAW"]), [], rows)
    assert rows == []
    observation.towers = observation.towers[:-1]
    quarantined = warmup.capture_tower_target(observation, info)
    assert quarantined["status"] == "quarantined"
    assert not warmup.validate_target(quarantined, raw)


def test_target_capture_and_sampling_do_not_advance_torch_rng():
    info = _real_information()
    obs = SimpleNamespace(owner=info.owner, tick=info.tick, towers=[SimpleNamespace(**t) for t in towers()])
    before = torch.get_rng_state().clone()
    python_before = random.getstate()
    for owner in (0, 1):
        for index in range(100):
            value_sample_gate(warmup.SEED, run_id="new", game_id="tower-training-0001",
                owner=owner, decision_index=index, probability=.5)
            warmup.capture_tower_target(obs, info)
    assert torch.equal(before, torch.get_rng_state())
    assert random.getstate() == python_before


def test_soft_ce_and_tail_actual_size_normalization():
    logits = torch.tensor([[.2, -.1], [-.2, .4], [.1, .1]], requires_grad=True)
    targets = torch.tensor([[.6, .4], [.25, .75], [.5, .5]])
    expected = -(targets * logits.log_softmax(-1)).sum()/3
    torch.testing.assert_close(warmup.soft_loss(logits, targets), expected)
    full = warmup.soft_loss(logits, targets)
    split = (2*warmup.soft_loss(logits[:2], targets[:2]) + warmup.soft_loss(logits[2:], targets[2:]))/3
    torch.testing.assert_close(full, split)
    torch.testing.assert_close(torch.autograd.grad(full, logits, retain_graph=True)[0],
        torch.autograd.grad(split, logits)[0])
    with pytest.raises(ValueError):
        warmup.soft_loss(logits, torch.ones_like(targets))


def test_real_cached_head_gradient_frozen_policy_and_checkpoint(tmp_path):
    model = initialize_fresh_model(warmup.SEED).eval()
    optimizer = warmup.create_optimizer(model)
    assert sum(p.numel() for p in model.parameters() if p.requires_grad) == sum(
        p.numel() for p in model.value_head.parameters())
    # state_dict intentionally omits nonpersistent catalog buffers; check those too.
    buffers_before = semantic_digest(dict(model.named_buffers()))
    info = _real_information()
    observation = SimpleNamespace(owner=info.owner, tick=info.tick,
        towers=[SimpleNamespace(**t) for t in towers()])
    raw = dict(**serialize_information(info), decision_index=3, value_gate_seed=5,
        action="wait", executed_action="wait", tower_hp_target=warmup.capture_tower_target(observation, info))
    cache = warmup.cache_rows(model, [ValueRow("g1", info, WLLabel.LOSS)], [raw])
    assert cache["schema"] == warmup.CACHE_SCHEMA
    assert cache["value_inputs"].shape == (1, 1024)
    assert not cache["value_inputs"].is_inference() and cache["cache_forward_max_abs_error"] == 0.
    frozen, policy = policy_function_sha(model), warmup.policy_signature(model, info)
    head = semantic_digest(model.value_head.state_dict())
    warmup.soft_loss(model.value_head(cache["value_inputs"]), torch.tensor([[.6, .4]])).backward()
    assert any(p.grad is not None and bool(p.grad.abs().sum() > 0) for p in model.value_head.parameters())
    assert all(p.grad is None for n, p in model.named_parameters() if not n.startswith("value_head."))
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    with torch.no_grad():
        full = model.value_only(info)
        batch = model.value_head(cache["value_inputs"].repeat(7, 1))
    torch.testing.assert_close(batch, full.expand(7, 2), atol=1e-6, rtol=0)
    assert policy_function_sha(model) == frozen and warmup.policy_signature(model, info) == policy
    assert semantic_digest(dict(model.named_buffers())) == buffers_before
    assert semantic_digest(model.value_head.state_dict()) != head
    save_checkpoint(tmp_path/"head.pt", model, optimizer, identity=dict(protocol=warmup.RUN_SCHEMA), optimizer_steps=1)
    restored = initialize_fresh_model(warmup.SEED)
    opt = warmup.create_optimizer(restored)
    load_checkpoint(tmp_path/"head.pt", restored, opt, expected_identity=dict(protocol=warmup.RUN_SCHEMA))
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == semantic_digest((restored.state_dict(), opt.state_dict()))
    assert semantic_digest(dict(restored.named_buffers())) == buffers_before
    with pytest.raises(ValueError, match="identity"):
        load_checkpoint(tmp_path/"head.pt", restored, opt, expected_identity=dict(protocol="wl-training"))


def synthetic_cache(model, prefix, n=257):
    generator = torch.Generator().manual_seed(12)
    x = torch.randn((n, 1024), generator=generator)
    scores = .1 * x[:, 0].tanh()
    return dict(schema=warmup.CACHE_SCHEMA, frozen_function_sha=policy_function_sha(model), value_inputs=x,
        records=[dict(game_id=f"{prefix}-{i%4}", score=float(v), soft_target=[float((1+v)/2), float((1-v)/2)])
            for i, v in enumerate(scores)])


def test_epoch_resume_matches_uninterrupted_best_model_and_optimizer(tmp_path, monkeypatch):
    monkeypatch.setattr(warmup, "MAX_EPOCHS", 4)
    model = initialize_fresh_model(warmup.SEED)
    optimizer = warmup.create_optimizer(model)
    train, valid = synthetic_cache(model, "train"), synthetic_cache(model, "validation", 31)
    identity = dict(protocol=warmup.RUN_SCHEMA)
    result = warmup.train_head(model, optimizer, train, valid,
        checkpoint_path=tmp_path/"full.pt", identity=identity)
    final = semantic_digest((model.state_dict(), optimizer.state_dict()))
    model2 = initialize_fresh_model(warmup.SEED)
    optimizer2 = warmup.create_optimizer(model2)
    def stop(_):
        raise KeyboardInterrupt("test committed epoch interruption")
    with pytest.raises(KeyboardInterrupt):
        warmup.train_head(model2, optimizer2, train, valid,
            checkpoint_path=tmp_path/"resume.pt", identity=identity, epoch_callback=stop)
    # Construct fresh objects, as a real process restart would.
    model2 = initialize_fresh_model(warmup.SEED)
    optimizer2 = warmup.create_optimizer(model2)
    resumed = warmup.train_head(model2, optimizer2, train, valid,
        checkpoint_path=tmp_path/"resume.pt", identity=identity)
    assert result == resumed
    assert semantic_digest((model2.state_dict(), optimizer2.state_dict())) == final
    assert all(e["samples"] == 257 and e["batches"] == 2 for e in result["epochs"])
    repeated = warmup.train_head(model2, optimizer2, train, valid,
        checkpoint_path=tmp_path/"resume.pt", identity=identity)
    assert repeated == resumed and semantic_digest((model2.state_dict(), optimizer2.state_dict())) == final


def test_best_model_optimizer_rollback_patience_and_empty_data(tmp_path, monkeypatch):
    model = initialize_fresh_model(warmup.SEED)
    optimizer = warmup.create_optimizer(model)
    train, valid = synthetic_cache(model, "train", 3), synthetic_cache(model, "valid", 2)
    before = semantic_digest((model.state_dict(), optimizer.state_dict()))
    monkeypatch.setattr(warmup, "score_metrics", lambda *_a, **_k: dict(mse=1.))
    result = warmup.train_head(model, optimizer, train, valid,
        checkpoint_path=tmp_path/"rollback.pt", identity={})
    assert result["status"] == "rolled_back" and result["early_stopped"]
    assert len(result["epochs"]) == 3 and result["selected_steps"] == 0
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == before
    train["records"], train["value_inputs"] = [], torch.empty((0, 1024))
    assert warmup.train_head(model, optimizer, train, valid, checkpoint_path=tmp_path/"empty.pt", identity={})["status"] == "skipped"
    assert not (tmp_path/"empty.pt").exists()


def test_best_epoch_optimizer_restored_and_overlap_rejected(tmp_path, monkeypatch):
    model = initialize_fresh_model(warmup.SEED)
    optimizer = warmup.create_optimizer(model)
    train, valid = synthetic_cache(model, "train", 3), synthetic_cache(model, "valid", 2)
    with pytest.raises(ValueError, match="overlap"):
        warmup.train_head(model, optimizer, train, train, checkpoint_path=tmp_path/"bad.pt", identity={})
    measurements = iter([1., .7, .8, .9, 1., .7])
    monkeypatch.setattr(warmup, "score_metrics", lambda *_a, **_k: dict(mse=next(measurements)))
    result = warmup.train_head(model, optimizer, train, valid, checkpoint_path=tmp_path/"best.pt", identity={})
    assert result["best_epoch"] == 1 and result["attempted_steps"] == 4
    assert all(int(state["step"]) == 1 for state in optimizer.state.values())


def test_metrics_equal_game_weighting_coverage_and_fixed_acceptance():
    rows = [dict(game_id="a", score=.1)] * 100 + [dict(game_id="b", score=.5)]
    result = warmup.score_metrics(rows, [0.]*101, replicates=100)
    assert result["mse"] == pytest.approx((.01+.25)/2)
    assert result["mae"] == pytest.approx(.3)
    assert result["correlation"] is None and result["advantage_games"] == 2
    assert not warmup.acceptance(result, result, result, integrity=True)["passed"]
    enough = [dict(game_id=str(i//25), score=.1 if i%2 else -.1) for i in range(100)]
    current = warmup.score_metrics(enough, [r["score"] for r in enough], replicates=100)
    baseline = warmup.score_metrics(enough, [0.]*100, replicates=100)
    assert warmup.acceptance(current, baseline, baseline, integrity=True)["passed"]
    assert not warmup.acceptance(current, baseline, baseline, integrity=False)["passed"]
    assert warmup.score_metrics([], [], replicates=100)["status"] == "undefined"


def test_nonzero_constant_correlation_is_undefined_despite_rounding():
    rows = [dict(game_id=str(i//333), score=.1 if i%2 else -.1) for i in range(2000)]
    report = warmup.score_metrics(rows, [-.00041045545114485737]*2000, replicates=10)
    assert report["correlation"] is None and report["correlation_defined_games"] == 0
    assert report["ci95"]["correlation"] is None
    assert all(game["correlation"] is None for game in report["per_game"])


def test_split_seed_domains_and_frozen_sizes():
    from native_runner.training.sspi.collector import episode_seed_identity, VALUE_ARM
    assert experiment.SPLITS == {"training":32, "validation":8, "test":8}
    domains = [{episode_seed_identity(warmup.SEED, run_id="tower-fixture", arm=VALUE_ARM,
        game_id=f"tower-{split}-{i:04d}")["actual_match_seed"] for i in range(1, count+1)}
        for split, count in experiment.SPLITS.items()]
    assert not domains[0]&domains[1] and not domains[0]&domains[2] and not domains[1]&domains[2]


def test_complete_runner_recovers_after_holdout_without_reevaluation(tmp_path, monkeypatch):
    monkeypatch.setattr(warmup, "MAX_EPOCHS", 1)
    def configure(run_dir, run_id, model, optimizer, **_):
        config = dict(run_id=run_id, source_sha="fixture", initial_model_sha=semantic_digest(model.state_dict()),
            frozen_sha=policy_function_sha(model), checkpoint_identity=dict(protocol=warmup.RUN_SCHEMA, run_id=run_id))
        experiment._atomic_json(run_dir/"run_config.json", config)
        return config
    monkeypatch.setattr(experiment, "_configuration", configure)
    def collect(run_dir, config, model, split, **_):
        cache = synthetic_cache(model, split, 10)
        receipt = dict(identity=dict(game_id=split), actual_match_seed=list(experiment.SPLITS).index(split))
        return cache, dict(receipts=[receipt]), []
    monkeypatch.setattr(experiment, "_collect_split", collect)
    write = experiment._atomic_json
    def crash(path, value):
        if path.name == "result.json":
            raise KeyboardInterrupt("after immutable holdout commit")
        write(path, value)
    monkeypatch.setattr(experiment, "_atomic_json", crash)
    with pytest.raises(KeyboardInterrupt):
        experiment.run(run_dir=tmp_path, run_id="fixture")
    holdout = (tmp_path/"holdout.json").read_bytes()
    checkpoint = (tmp_path/"checkpoints/best.pt").read_bytes()
    monkeypatch.setattr(experiment, "_atomic_json", write)
    real_metrics = warmup.score_metrics
    def no_holdout(*a, **k):
        assert k.get("replicates") == 0, "held-out metrics were repeated"
        return real_metrics(*a, **k)
    monkeypatch.setattr(warmup, "score_metrics", no_holdout)
    result = experiment.run(run_dir=tmp_path, run_id="fixture")
    assert result["status"] == "complete" and result["integrity"]["checkpoint_roundtrip"]
    assert (tmp_path/"holdout.json").read_bytes() == holdout
    assert (tmp_path/"checkpoints/best.pt").read_bytes() == checkpoint
    monkeypatch.setattr(experiment, "_collect_split", lambda *_a, **_k: pytest.fail("completed run recollected"))
    assert experiment.run(run_dir=tmp_path, run_id="fixture") == result
