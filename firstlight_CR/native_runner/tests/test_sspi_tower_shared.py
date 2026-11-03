"""Unfrozen tower fitting: batch equivalence, gradient scope and recovery."""

from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_sspi_search_performance import _real_information
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.v4.tensors import concatenate_padded_tensor_records
from native_runner.training.sspi.model import initialize_fresh_model, _hand_features
from native_runner.training.sspi import tower_shared as shared
from native_runner.training.sspi.tower_bootstrap import soft_loss
from native_runner.training.sspi.value_learning import policy_head_sha, policy_function_sha


@pytest.fixture(autouse=True)
def one_thread():
    torch.set_num_threads(1)


def example_batch():
    info = _real_information()
    features = info.frame.towers.features.clone()
    features[0,0,0] *= .8
    features[0,0,5] *= .8
    second = replace(info, frame=replace(info.frame,
        towers=replace(info.frame.towers, features=features)))
    frame = concatenate_padded_tensor_records((info.frame, second.frame))
    hands = torch.stack([_hand_features(row.hand, device="cpu") for row in (info,second)])
    return (info,second), frame, hands


def test_batch_value_matches_individual_and_preserves_initial_identity():
    model = initialize_fresh_model(20261103).eval()
    assert semantic_digest(model.state_dict()) == "14fbd5fa43ba84be860f4333f3c63a935879b2ca0d6221978100c93cb6061415"
    rows, frame, hands = example_batch()
    with torch.no_grad():
        individual = torch.cat([model.value_only(row) for row in rows])
        batch = model.value_batch(frame, hands)
    torch.testing.assert_close(batch, individual, atol=1e-6, rtol=1e-5)
    with pytest.raises(ValueError, match="one hand"):
        model.value_batch(frame, hands[:1])


def test_batch_gradients_match_individual_full_backbone():
    model = initialize_fresh_model(20261103).train()
    shared.create_optimizer(model)
    rows, frame, hands = example_batch()
    targets = torch.tensor([[.6,.4],[.3,.7]])
    soft_loss(torch.cat([model.value_only(row) for row in rows]), targets).backward()
    reference = {name:p.grad.clone() for name,p in model.named_parameters() if p.grad is not None}
    model.zero_grad(set_to_none=True)
    soft_loss(model.value_batch(frame,hands), targets).backward()
    assert reference
    for name,p in model.named_parameters():
        if name.startswith("policy_head."):
            assert p.grad is None
        elif name in reference:
            torch.testing.assert_close(p.grad,reference[name],atol=2e-6,rtol=3e-3)


def test_shared_value_update_and_checkpoint_restore(tmp_path, monkeypatch):
    model = initialize_fresh_model(20261103)
    optimizer = shared.create_optimizer(model)
    assert sum(p.numel() for p in model.parameters() if p.requires_grad) == 7055045
    _, frame, hands = example_batch()
    data = dict(frame=frame,hands=hands,targets=torch.tensor([[.6,.4],[.3,.7]]),
        records=[dict(game_id="g1",score=.2),dict(game_id="g1",score=-.4)])
    policy = policy_head_sha(model)
    backbone = policy_function_sha(model)
    buffers = semantic_digest(dict(model.named_buffers()))
    head = semantic_digest(model.value_head.state_dict())
    monkeypatch.setattr(shared,"BATCH",2)
    monkeypatch.setattr(shared,"MICROBATCH",1)
    result = shared.train_epoch(model,optimizer,data,1,device="cpu")
    assert result["samples"] == 2 and result["steps"] == 1
    assert all(v > 0 for v in result["first_batch_gradient_l1"].values())
    assert policy_head_sha(model) == policy
    assert semantic_digest(dict(model.named_buffers())) == buffers
    assert policy_function_sha(model) != backbone and semantic_digest(model.value_head.state_dict()) != head
    state = dict(epoch=1,steps=1)
    shared._save(tmp_path/"resume.pt",model,optimizer,dict(protocol=shared.RUN_SCHEMA),state)
    reference = semantic_digest((model.state_dict(),optimizer.state_dict()))
    fresh = initialize_fresh_model(20261103)
    opt = shared.create_optimizer(fresh)
    restored = shared._load(tmp_path/"resume.pt",fresh,opt,dict(protocol=shared.RUN_SCHEMA),"cpu")
    assert restored["epoch"] == 1 and restored["cuda_rng"] is None
    assert semantic_digest((fresh.state_dict(),opt.state_dict())) == reference


def test_microbatch_tail_matches_single_effective_batch(monkeypatch):
    rows, frame, hands = example_batch()
    data = dict(frame=frame,hands=hands,targets=torch.tensor([[.6,.4],[.3,.7]]),
        records=[dict(game_id="g1",score=.2),dict(game_id="g1",score=-.4)])
    first,second = initialize_fresh_model(20261103),initialize_fresh_model(20261103)
    opt1,opt2 = shared.create_optimizer(first),shared.create_optimizer(second)
    monkeypatch.setattr(shared,"BATCH",256) # Two-sample tail must divide by two, not 256.
    monkeypatch.setattr(shared,"MICROBATCH",1)
    shared.train_epoch(first,opt1,data,1,device="cpu")
    monkeypatch.setattr(shared,"MICROBATCH",2)
    shared.train_epoch(second,opt2,data,1,device="cpu")
    for a,b in zip(first.parameters(),second.parameters()):
        torch.testing.assert_close(a,b,atol=2e-6,rtol=1e-4)


def test_signal_requires_both_splits_and_direction_coverage():
    constants=dict(training=.01,validation=.01)
    train=dict(mse=.003)
    valid=dict(mse=.003,mae=.03,direction_accuracy=.9,advantage_games=6,advantage_states=1000)
    assert shared.fit_signal(train,valid,constants)
    assert not shared.fit_signal(dict(mse=.009),valid,constants)
    assert not shared.fit_signal(train,dict(valid,mse=.009),constants)
    assert not shared.fit_signal(train,dict(valid,direction_accuracy=.6),constants)
    assert not shared.fit_signal(train,dict(valid,advantage_states=20),constants)
    state=dict(streak=3,best_metrics=dict(training=train,validation=valid),constants=constants)
    assert shared.fitting_complete(state) # Resume after the last committed epoch needs no new update.
    assert not shared.fitting_complete(dict(state,streak=2))


def test_checkpoint_resume_replays_next_epoch_exactly(tmp_path, monkeypatch):
    model = initialize_fresh_model(20261103)
    optimizer = shared.create_optimizer(model)
    _,frame,hands = example_batch()
    data = dict(frame=frame,hands=hands,targets=torch.tensor([[.6,.4],[.3,.7]]),
        records=[dict(game_id="g1",score=.2),dict(game_id="g1",score=-.4)])
    monkeypatch.setattr(shared,"MICROBATCH",1)
    shared.train_epoch(model,optimizer,data,1,device="cpu")
    identity=dict(protocol=shared.RUN_SCHEMA)
    shared._save(tmp_path/"resume.pt",model,optimizer,identity,dict(epoch=1,steps=1))
    update = shared.train_epoch(model,optimizer,data,2,device="cpu")
    expected = semantic_digest((model.state_dict(),optimizer.state_dict()))
    fresh = initialize_fresh_model(20261103)
    opt = shared.create_optimizer(fresh)
    shared._load(tmp_path/"resume.pt",fresh,opt,identity,"cpu")
    actual = shared.train_epoch(fresh,opt,data,2,device="cpu")
    assert update == actual
    assert semantic_digest((fresh.state_dict(),opt.state_dict())) == expected


def test_cached_representation_schema_is_rejected(tmp_path):
    import json
    (tmp_path/"dataset.json").write_text(json.dumps(dict(identity=dict(schema="sspi-tower-head-cache.v1"))))
    with pytest.raises(ValueError,match="raw supervised"):
        shared.load_data(tmp_path)
