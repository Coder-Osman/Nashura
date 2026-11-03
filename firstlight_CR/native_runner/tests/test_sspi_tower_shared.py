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
    assert shared.fitting_complete(state) # Fit detection is separate from the eight-epoch budget.
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


def test_refresh_budget_never_starts_a_ninth_epoch():
    assert shared.MAX_EPOCHS == 8
    assert shared.batch_has_work(dict(epoch=0))
    assert shared.batch_has_work(dict(epoch=7))
    assert not shared.batch_has_work(dict(epoch=8))
    assert not shared.batch_has_work(dict(epoch=11))


def test_parent_carries_model_and_adam_and_rejects_consumed_data(tmp_path):
    model=initialize_fresh_model(20261103)
    optimizer=shared.create_optimizer(model)
    _,frame,hands=example_batch()
    data=dict(frame=frame,hands=hands,targets=torch.tensor([[.6,.4],[.3,.7]]),
        records=[dict(game_id="g1",score=.2),dict(game_id="g1",score=-.4)])
    shared.train_epoch(model,optimizer,data,1,device="cpu")
    identity=dict(protocol="sspi-tower-shared-fit.v1",seed=20261103,dataset_sha="old-data",
        policy_frozen=True,shared_frozen=False)
    path=tmp_path/"parent.pt"
    shared._save(path,model,optimizer,identity,dict(epoch=1,steps=1,consumed_datasets=["older-data"]))
    expected=semantic_digest((model.state_dict(),optimizer.state_dict()))
    fresh=initialize_fresh_model(20261103);opt=shared.create_optimizer(fresh)
    receipt=shared.restore_parent(path,shared.file_sha(path),fresh,opt,device="cpu",dataset_sha="fresh-data")
    assert semantic_digest((fresh.state_dict(),opt.state_dict()))==expected
    assert receipt["steps"]==1 and receipt["consumed_datasets"]==["older-data","old-data"]
    for reused in ("older-data","old-data"):
        with pytest.raises(ValueError,match="already consumed"):
            shared.restore_parent(path,shared.file_sha(path),fresh,opt,device="cpu",dataset_sha=reused)
    with pytest.raises(ValueError,match="checksum"):
        shared.restore_parent(path,"wrong",fresh,opt,device="cpu",dataset_sha="fresh-data")


def test_fresh_batches_have_disjoint_training_validation_and_prior_seeds():
    from native_runner.training.sspi.tower_refresh import planned_games
    first=planned_games("fresh-b0001")
    second=planned_games("fresh-b0002",first)
    assert len(first)==len(second)==40
    assert sum(r["split"]=="training" for r in first)==32
    assert sum(r["split"]=="validation" for r in first)==8
    for key in ("actual_match_seed","actual_rollout_seed"):
        assert len({r[key] for r in first+second})==80
    with pytest.raises(ValueError,match="repeat a native seed"):
        planned_games("fresh-b0001",first)


def test_eight_epoch_run_and_boundary_recovery_do_not_repeat_exposure(tmp_path,monkeypatch):
    import json
    model=initialize_fresh_model(20261103)
    _,frame,hands=example_batch()
    data={s:dict(frame=frame,hands=hands,targets=torch.tensor([[.6,.4],[.3,.7]]),
        records=[dict(game_id=s,score=.2),dict(game_id=s,score=-.4)])
        for s in ("training","validation")}
    manifest=dict(identity=dict(initial_model_sha=semantic_digest(model.state_dict())))
    data_dir=tmp_path/"data";data_dir.mkdir()
    (data_dir/"dataset.json").write_text(json.dumps(manifest))
    torch.save(dict(architecture=model.architecture,model=model.state_dict()),data_dir/"initial.pt")
    monkeypatch.setattr(shared,"load_data",lambda _: (data,[],manifest))
    calls=[];real_epoch=shared.train_epoch
    def counted(*args,**kwargs):
        calls.append(args[3]);return real_epoch(*args,**kwargs)
    monkeypatch.setattr(shared,"train_epoch",counted)
    run_dir=tmp_path/"run"
    first=shared.run(data_dir,run_dir,"bounded-fixture",device="cpu")
    assert first["status"]=="batch_budget_complete" and first["epochs"]==8
    assert first["batch_update_steps"]==8 and calls==list(range(1,9))
    assert first["checkpoint_roundtrip"]
    # Completed invocation is idempotent; recovery after the committed eighth epoch is too.
    assert shared.run(data_dir,run_dir,"bounded-fixture",device="cpu")==first
    (run_dir/"result.json").unlink()
    second=shared.run(data_dir,run_dir,"bounded-fixture",device="cpu")
    assert calls==list(range(1,9)) and second["epochs"]==8
    assert second["model_sha"]==first["model_sha"]
    assert second["optimizer_sha"]==first["optimizer_sha"]


def coordinator_module():
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[3]/"tools/run_tower_refresh.py"
    spec=importlib.util.spec_from_file_location("tower_coordinator_test",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_fresh_coordinator_changes_data_and_carries_selected_parent(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from native_runner.training.sspi.tower_refresh import planned_games
    module=coordinator_module()
    for name in ("initial","probe"):(tmp_path/name).write_text(name)
    (tmp_path/"excluded").write_text("[]")
    args=SimpleNamespace(series_dir=tmp_path,series_id="fresh-fixture",initial_model=tmp_path/"initial",
        probe=tmp_path/"probe",excluded_seeds=tmp_path/"excluded",parent_checkpoint="/remote/initial.pt",
        parent_sha="initial-sha",remote="training-host",remote_root="/remote",remote_python="python")
    seen=[];parents=[]
    def collect(args,batch,run_id,excluded):
        batch.mkdir(exist_ok=True)
        plan=planned_games(run_id,json.loads(excluded.read_text()))
        seen.extend(plan)
        (batch/"run_config.json").write_text(json.dumps(dict(planned_games=plan)))
    def train(args,batch,run_id,parent,parent_sha):
        parents.append((parent,parent_sha))
        index=len(parents)
        if index==2:
            (tmp_path/"STOP_AFTER_BATCH").touch()
        return "/remote/"+batch.name,dict(status="fitting_observed" if index==2 else "batch_budget_complete",
            checkpoint_sha=f"checkpoint-{index}")
    monkeypatch.setattr(module,"collect_batch",collect)
    monkeypatch.setattr(module,"train_batch",train)
    module.run(args)
    assert len(seen)==80 and len({r["actual_match_seed"] for r in seen})==80
    assert parents==[("/remote/initial.pt","initial-sha"),("/remote/batch-0001/run/best.pt","checkpoint-1")]
    assert json.loads((tmp_path/"status.json").read_text())==dict(status="paused_user",next_batch=3)


def test_series_identity_allows_only_coordinator_code_revision():
    module=coordinator_module()
    first=dict(series_id="same-series",source_sha="source",coordinator_sha="old")
    revised=dict(series_id="same-series",source_sha="source",coordinator_sha="new")
    changed=dict(revised,source_sha="different")
    assert module.compatible_series_identity(first,revised)
    assert not module.compatible_series_identity(first,changed)


def test_continuity_watcher_restarts_only_after_fit_completion():
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[3]/"tools/watch_tower_refresh.py"
    spec=importlib.util.spec_from_file_location("tower_continuity_test",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.should_resume(dict(status="complete",stage="fitting_observed"))
    assert not module.should_resume(dict(status="paused_failure",stage="error"))
    assert not module.should_resume(dict(status="running",stage="remote_training"))
    assert module.should_stop(dict(status="paused_failure"))
    assert module.is_coordinator(["python","-u","/tmp/run_tower_refresh.py",
        "--series-id","series"],"series")
    assert not module.is_coordinator(["python","/tmp/watch_tower_refresh.py",
        "--series-id","series","--restart-frozen-source","/tmp/run_tower_refresh.py"],"series")


def test_continuity_watcher_can_switch_to_updated_frozen_coordinator():
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[3]/"tools/watch_tower_refresh.py"
    spec=importlib.util.spec_from_file_location("tower_continuity_command_test",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    old="/tmp/frozen-old"
    new="/tmp/frozen-updated"
    command=["python3","-u",old+"/tools/run_tower_refresh.py","--series-id","series",
        "--frozen-source",old,"--series-dir","/tmp/series"]
    rewritten=module.coordinator_command(command,new)
    assert rewritten[2]==new+"/tools/run_tower_refresh.py"
    assert rewritten[rewritten.index("--frozen-source")+1]==new
    assert rewritten[rewritten.index("--series-dir")+1]=="/tmp/series"
    assert command[2]==old+"/tools/run_tower_refresh.py"


def test_coordinator_rejects_wrong_budget_or_unverified_checkpoint():
    module=coordinator_module()
    report=dict(schema=shared.RUN_SCHEMA,epochs=8,status="batch_budget_complete",
        identity=dict(max_epochs=8,dataset_sha="fresh-data",source_sha=shared.source_sha()),
        policy_head_unchanged=True,buffers_unchanged=True,checkpoint_roundtrip=True)
    module.validate_training_receipt(report,"fresh-data")
    for changed in (dict(report,epochs=9),dict(report,checkpoint_roundtrip=False)):
        with pytest.raises(ValueError,match="eight-epoch receipt"):
            module.validate_training_receipt(changed,"fresh-data")
    with pytest.raises(ValueError,match="eight-epoch receipt"):
        module.validate_training_receipt(report,"different-data")
