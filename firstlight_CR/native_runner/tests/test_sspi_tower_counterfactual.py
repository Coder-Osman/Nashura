from dataclasses import fields, is_dataclass, replace
import json

import pytest
import torch
from torch import nn

from native_runner.tests.test_sspi_search_performance import _real_information
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi import tower_counterfactual as counterfactual
from native_runner.training.sspi.tower_bootstrap import tower_target
from native_runner.training.sspi.learning import load_checkpoint, save_checkpoint
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.value_learning import configure_value_training, policy_head_sha
from native_runner.training.v4.tensors import TowerSetV4


def _information_and_target(*, destroyed=(), owner=0):
    information = _real_information()
    information = replace(information, owner=owner)
    tensor_types = (0, 2, 1, 0, 2, 1) if owner == 1 else (0, 1, 2, 0, 1, 2)
    tower_type = torch.tensor([tensor_types], dtype=torch.long)
    owner_type = torch.tensor([[0, 0, 0, 1, 1, 1]], dtype=torch.long)
    features = torch.zeros((1, 6, 21), dtype=torch.float32)
    rows = []
    hp_by_row = (5000., 3500., 3500., 5000., 2500., 2500.)
    kinds = ("king", "princess_left", "princess_right") * 2
    owners = (owner, owner, owner, 1 - owner, 1 - owner, 1 - owner)
    for index, (owner, kind, hp) in enumerate(zip(owners, kinds, hp_by_row)):
        maximum = 5000.
        if (owner, kind) in destroyed:
            hp = 0.
        features[0, index, 0] = hp / maximum
        features[0, index, 2] = float(hp > 0.)
        features[0, index, 4] = 0. if kind == "king" else 1.
        features[0, index, 5] = hp / 5000.
        features[0, index, 6] = maximum / 5000.
        rows.append(dict(owner=owner, tower_kind=kind, hitpoints=hp,
            max_hitpoints=maximum, active=hp > 0.))
    towers = TowerSetV4(tower_type=tower_type,
        tower_troop_type=torch.zeros((1, 6), dtype=torch.long), owner_type=owner_type,
        features=features, position=torch.zeros((1, 6, 2)),
        extent=torch.zeros((1, 6, 4)), mask=torch.ones((1, 6), dtype=torch.bool))
    information = replace(information, frame=replace(information.frame, towers=towers))
    target = dict(owner=information.owner, tick=information.tick,
        fair_sha256=information.sha256, towers=rows,
        **tower_target(rows, information.owner))
    return information, target


def _tensor_fields(value, prefix=()):
    if isinstance(value, torch.Tensor):
        yield prefix, value
    elif is_dataclass(value):
        for item in fields(value):
            yield from _tensor_fields(getattr(value, item.name), (*prefix, item.name))


def test_episode_split_is_deterministic_disjoint_and_matches_fixed_ratios():
    sources = [dict(source_id=f"run/game-{index:03d}") for index in range(288)]
    first = counterfactual.split_sources(sources)
    assert first == counterfactual.split_sources(list(reversed(sources)))
    assert {name: list(first.values()).count(name)
        for name in counterfactual.SPLIT_NAMES} == {
            "training": 230, "validation": 29, "heldout": 29}
    assert set(first) == {source["source_id"] for source in sources}


def test_hp_intervention_changes_only_ratio_and_absolute_fields():
    information, target = _information_and_target()
    before = semantic_digest(target)
    values, score = counterfactual.hp_values_for_score(information, target, .1,
        (0.25, .6, 1., 1., .6, .25), synthetic=True)
    changed = counterfactual.replace_tower_hp(information, target, values)
    assert semantic_digest(target) == before
    assert changed.hand == information.hand and changed.legality is information.legality
    assert changed.owner == information.owner and changed.tick == information.tick
    assert score == pytest.approx(.1, abs=1e-5)
    assert counterfactual.tower_target(target["towers"], information.owner)["score"] == target["score"]
    for path, original in _tensor_fields(information.frame):
        updated = dict(_tensor_fields(changed.frame))[path]
        if path == ("towers", "features"):
            assert not torch.equal(updated[..., 0], original[..., 0])
            assert not torch.equal(updated[..., 5], original[..., 5])
            restored = updated.clone()
            restored[..., 0] = original[..., 0]
            restored[..., 5] = original[..., 5]
            assert torch.equal(restored, original)
        else:
            assert torch.equal(updated, original), path
    rows, _ = counterfactual.tower_rows_by_key(information, target)
    for index, key in enumerate(counterfactual.TOWER_KEYS):
        row = rows[key]
        assert changed.frame.towers.features[0, row, 0] == pytest.approx(
            values[index] / target["towers"][row]["max_hitpoints"])
        assert changed.frame.towers.features[0, row, 5] == pytest.approx(values[index] / 5000.)


def test_counterfactual_preserves_destroyed_and_inactive_king_states():
    information, target = _information_and_target(destroyed={(0, "princess_left")})
    values, _ = counterfactual.hp_values_for_score(information, target, .05,
        (1., .25, .6, 1.75, .6, .25))
    assert values[counterfactual.TOWER_KEYS.index((0, "princess_left"))] == 0.
    assert values[counterfactual.TOWER_KEYS.index((0, "king"))] == 5000.
    variants = counterfactual.counterfactual_variants(information, target, "game-1", 7)
    assert variants == counterfactual.counterfactual_variants(information, target, "game-1", 7)
    for variant in variants:
        counterfactual.validate_variant(information, target, variant)
        assert variant["tower_hp"][counterfactual.TOWER_KEYS.index((0, "king"))] == 5000.
        assert variant["tower_hp"][counterfactual.TOWER_KEYS.index((0, "princess_left"))] == 0.
    invalid = dict(variants[0], tower_hp=list(variants[0]["tower_hp"]))
    invalid["tower_hp"][counterfactual.TOWER_KEYS.index((0, "king"))] -= 1.
    with pytest.raises(ValueError):
        counterfactual.validate_variant(information, target, invalid)


def test_hp_scores_and_ranges_follow_the_current_player_perspective():
    information, target = _information_and_target(owner=1)
    bounds = counterfactual._hp_bounds(information, target)[2]
    side_zero = counterfactual._score_range(bounds, 0)
    side_one = counterfactual._score_range(bounds, 1)
    assert side_one == pytest.approx((-side_zero[1], -side_zero[0]))
    values, score = counterfactual.hp_values_for_score(information, target, .1,
        (0.25, .6, 1., 1., .6, .25))
    assert score == pytest.approx(.1, abs=1e-5)
    assert counterfactual._target_from_hp(target, information.owner, values)["score"] == pytest.approx(.1)


class _TinyTowerModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = nn.Linear(1, 4)
        self.hand_projection = nn.Linear(128, 4)
        self.trunk = nn.Linear(8, 4)
        self.value_head = nn.Linear(4, 2)
        self.policy_head = nn.Linear(4, 3)

    def value_batch(self, frame, hands):
        tower = frame.towers.features[:, :, 0].mean(dim=1, keepdim=True)
        scene = self.encoder(tower)
        hand = self.hand_projection(hands)
        hidden = torch.tanh(self.trunk(torch.cat((scene, hand), dim=-1)))
        return self.value_head(hidden)


def test_real_example_materializes_hp_variant_and_updates_value_path_only():
    torch.manual_seed(13)
    information, target = _information_and_target()
    variants = counterfactual.counterfactual_variants(information, target, "game-1", 1)
    variant = variants[0]
    state = counterfactual.TowerState("game-1", 1, information, target, variants)
    examples = list(counterfactual._real_examples([state]))
    assert len(examples) == 4
    assert examples[0].hp_by_key is None and examples[1].hp_by_key == variant["tower_hp"]
    original, changed = counterfactual._example_frame(examples[0]), counterfactual._example_frame(examples[1])
    assert not torch.equal(original.towers.features[..., 0], changed.towers.features[..., 0])

    model = _TinyTowerModel()
    trainable = configure_value_training(model, head_only=False)
    optimizer = torch.optim.AdamW(trainable, lr=counterfactual.LR,
        weight_decay=counterfactual.WEIGHT_DECAY)
    frozen_policy = policy_head_sha(model)
    result = counterfactual.train_examples(model, optimizer, examples[1:2],
        epoch=1, device="cpu", seed_key=5)
    assert result["samples"] == 1 and result["steps"] == 1
    assert policy_head_sha(model) == frozen_policy
    assert all(parameter.grad is None for parameter in model.policy_head.parameters())
    assert all(parameter.grad is None for name, parameter in model.named_parameters()
        if name.startswith(("encoder.", "hand_projection.", "trunk.", "value_head.")))
    assert result["first_batch_gradient_l1"]


def test_real_state_ids_distinguish_both_players_at_matching_decision_indices():
    info_zero, target_zero = _information_and_target(owner=0)
    info_one, target_one = _information_and_target(owner=1)
    states = [counterfactual.TowerState("game-1", 5, info_zero, target_zero,
                counterfactual.counterfactual_variants(info_zero, target_zero, "game-1", 5)),
        counterfactual.TowerState("game-1", 5, info_one, target_one,
                counterfactual.counterfactual_variants(info_one, target_one, "game-1", 5))]
    ids = [example.state_id for example in counterfactual._real_examples(states)]
    assert len(ids) == len(set(ids)) == 8


def _run_stage_once(stage_dir, *, interrupt_after_first_epoch=False, monkeypatch=None):
    model = initialize_fresh_model(counterfactual.SEED)
    optimizer = counterfactual._make_optimizer(model)
    identity = dict(protocol=counterfactual.RUN_SCHEMA, run_id="checkpoint-stage-test",
        stage="synthetic", seed=counterfactual.SEED)
    original_atomic_json = counterfactual._atomic_json
    if interrupt_after_first_epoch:
        def fail_after_saved_epoch(path, value):
            if path.name == "status.json" and value.get("status") == "epoch_complete":
                raise RuntimeError("simulated interruption after durable epoch checkpoint")
            return original_atomic_json(path, value)
        monkeypatch.setattr(counterfactual, "_atomic_json", fail_after_saved_epoch)

    def train_epoch(epoch):
        optimizer.zero_grad(set_to_none=True)
        bias = model.value_head[-1].bias
        loss = (bias[0] - 1.) ** 2
        loss.backward()
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        return dict(epoch=epoch, steps=1, samples=1)

    def validation(_model):
        error = float((model.value_head[-1].bias[0].detach() - 1.).square())
        return dict(mse=error, mae=error ** .5)

    try:
        result = counterfactual._run_stage(stage_dir, model, optimizer, identity,
            train_epoch=train_epoch, validation=validation, max_epochs=2)
        return model, optimizer, result
    finally:
        if interrupt_after_first_epoch:
            monkeypatch.setattr(counterfactual, "_atomic_json", original_atomic_json)


def test_training_stage_resumes_durable_model_and_adamw_checkpoint(tmp_path, monkeypatch):
    stage_dir = tmp_path / "resumed"
    with pytest.raises(RuntimeError, match="durable epoch checkpoint"):
        _run_stage_once(stage_dir, interrupt_after_first_epoch=True, monkeypatch=monkeypatch)
    resumed_model, resumed_optimizer, resumed_report = _run_stage_once(stage_dir)
    full_model, full_optimizer, full_report = _run_stage_once(tmp_path / "full")
    assert resumed_report["epochs"] == full_report["epochs"] == 2
    assert resumed_report["optimizer_steps"] == full_report["optimizer_steps"] == 2
    assert resumed_report["checkpoint_roundtrip"] is True
    assert semantic_digest((resumed_model.state_dict(), resumed_optimizer.state_dict())) == semantic_digest(
        (full_model.state_dict(), full_optimizer.state_dict()))


def test_run_identity_archives_only_failed_precheckpoint_source_revision(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    previous = dict(run_id="counterfactual-retry", source_sha="old-source", device="mps")
    current = dict(run_id="counterfactual-retry", source_sha="new-source", device="mps")
    counterfactual._write_json(run_dir / "run_config.json", previous)
    counterfactual._write_json(run_dir / "status.json", dict(status="paused_failure",
        reason="validation callback signature"))
    (run_dir / "backend-probe.json").write_text("probe evidence\n")

    counterfactual._activate_run_identity(run_dir, current)

    archive = run_dir / "attempts" / "attempt-001"
    assert json.loads((run_dir / "run_config.json").read_text()) == current
    assert json.loads((archive / "run_config.json").read_text()) == previous
    assert json.loads((archive / "status.json").read_text())["reason"] == "validation callback signature"
    assert (archive / "backend-probe.json").read_text() == "probe evidence\n"


def test_run_identity_rejects_other_immutable_changes(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    previous = dict(run_id="counterfactual-retry", source_sha="old-source", device="mps")
    current = dict(run_id="counterfactual-retry", source_sha="new-source", device="mps")
    current["device"] = "cpu"
    counterfactual._write_json(run_dir / "run_config.json", previous)
    counterfactual._write_json(run_dir / "status.json", dict(status="paused_failure"))

    with pytest.raises(ValueError, match="immutable identity"):
        counterfactual._activate_run_identity(run_dir, current)
    assert not (run_dir / "attempts").exists()


def test_run_identity_rejects_revision_change_after_checkpoint(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "stage-synthetic").mkdir(parents=True)
    previous = dict(run_id="counterfactual-retry", source_sha="old-source", device="mps")
    current = dict(run_id="counterfactual-retry", source_sha="new-source", device="mps")
    counterfactual._write_json(run_dir / "run_config.json", previous)
    counterfactual._write_json(run_dir / "status.json", dict(status="paused_failure"))
    (run_dir / "stage-synthetic" / "latest.pt").write_bytes(b"checkpoint")

    with pytest.raises(ValueError, match="immutable identity"):
        counterfactual._activate_run_identity(run_dir, current)
    assert json.loads((run_dir / "run_config.json").read_text()) == previous


def test_dataset_catalog_reuse_allows_trainer_source_revision_only(monkeypatch):
    catalog = dict(schema=counterfactual.DATA_SCHEMA, seed=counterfactual.SEED,
        source_run_ids=["source-run"], initial_model_sha="initial-model",
        source_sha="current-source", split_sizes={"training": 1, "validation": 0, "heldout": 0},
        sources=[dict(source_id="source-run/game-1", split="training", samples=12)])
    recorded_catalog = dict(catalog, source_sha="dataset-source")
    source = dict(catalog["sources"][0], augmentation={"path": "unused.json"})
    manifest = dict(identity=dict(schema=counterfactual.DATA_SCHEMA, seed=counterfactual.SEED,
        source_runs=catalog["source_run_ids"], initial_model_sha=catalog["initial_model_sha"],
        source_sha="dataset-source", split_sizes=catalog["split_sizes"],
        catalog_sha=semantic_digest(recorded_catalog)), sources=[source])
    monkeypatch.setattr(counterfactual, "source_sha", lambda: "current-source")

    assert counterfactual._manifest_catalog_matches(manifest, catalog)

    catalog["sources"][0]["samples"] += 1
    assert not counterfactual._manifest_catalog_matches(manifest, catalog)


def test_backend_selector_prefers_verified_cuda_and_falls_back_by_speed():
    assert counterfactual._select_backend({
        "cpu": {"status": "passed", "median_seconds": .2},
        "mps": {"status": "passed", "median_seconds": .1},
        "cuda": {"status": "passed", "median_seconds": .3},
    }) == "cuda"
    assert counterfactual._select_backend({
        "cpu": {"status": "passed", "median_seconds": .2},
        "mps": {"status": "passed", "median_seconds": .1},
        "cuda": {"status": "failed", "median_seconds": .01},
    }) == "mps"
    with pytest.raises(RuntimeError, match="CUDA was required"):
        counterfactual._select_backend({
            "cpu": {"status": "passed", "median_seconds": .2},
            "mps": {"status": "passed", "median_seconds": .1},
            "cuda": {"status": "failed", "median_seconds": .01},
        }, require_cuda=True)


def test_initial_checkpoint_preserves_exact_seed_across_torch_versions(tmp_path):
    model = initialize_fresh_model(counterfactual.SEED)
    optimizer = counterfactual._make_optimizer(model)
    identity = dict(protocol=counterfactual.RUN_SCHEMA, run_id="seed-source")
    checkpoint = save_checkpoint(tmp_path / "seed.pt", model, optimizer,
        identity=identity, optimizer_steps=0, run_state={"steps": 0})
    restored = initialize_fresh_model(counterfactual.SEED, device="cpu")
    state, file_sha = counterfactual._read_initial_model_checkpoint(
        tmp_path / "seed.pt", restored, checkpoint["model_sha"])

    assert file_sha == checkpoint["file_sha"]
    assert semantic_digest(state) == checkpoint["model_sha"]
    assert semantic_digest(restored.state_dict()) == checkpoint["model_sha"]
    with pytest.raises(ValueError, match="zero-update seed"):
        counterfactual._read_initial_model_checkpoint(tmp_path / "seed.pt", restored, "wrong")


def test_checkpoint_roundtrip_keeps_the_full_model_and_optimizer_identity(tmp_path):
    model = initialize_fresh_model(counterfactual.SEED)
    optimizer = counterfactual._make_optimizer(model)
    optimizer.zero_grad(set_to_none=True)
    model.value_head[-1].bias[0].backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    identity = dict(protocol=counterfactual.RUN_SCHEMA, run_id="counterfactual-test")
    checkpoint = save_checkpoint(tmp_path / "tower.pt", model, optimizer,
        identity=identity, optimizer_steps=1)
    restored = initialize_fresh_model(counterfactual.SEED)
    restored_optimizer = counterfactual._make_optimizer(restored)
    result = load_checkpoint(tmp_path / "tower.pt", restored, restored_optimizer,
        expected_identity=identity)
    saved_payload = torch.load(tmp_path / "tower.pt", weights_only=True, map_location="cpu")["payload"]
    assert result["file_sha"] == checkpoint["file_sha"]
    assert saved_payload.get("cuda_rng") is None if not torch.cuda.is_available() else len(
        saved_payload["cuda_rng"]) == torch.cuda.device_count()
    assert semantic_digest((restored.state_dict(), restored_optimizer.state_dict())) == semantic_digest(
        (model.state_dict(), optimizer.state_dict()))
