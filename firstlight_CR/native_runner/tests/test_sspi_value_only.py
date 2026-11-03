import hashlib
import json
from pathlib import Path

import pytest
import torch
from torch import nn

from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi.augmentation import serialize_information
from native_runner.training.sspi.collector import (VALUE_ARM, VALUE_EPISODE_SCHEMA,
    value_sample_gate)
from native_runner.training.sspi.learning import (WLLabel, load_checkpoint,
    save_checkpoint)
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.protocol import HandState, SSPILegality, SSPIInformation
from native_runner.training.sspi.value_learning import (ValueRow,
    create_value_optimizer, load_value_episode, policy_head_sha,
    prediction_metrics, train_value_generation)


class TinyValueModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.shared = nn.Linear(2, 4)
        self.policy_head = nn.Linear(4, 3)
        self.value_head = nn.Linear(4, 2)

    def value_only(self, information):
        return self.value_head(self.shared(information).tanh())


def test_value_gate_is_deterministic_and_independent_by_owner_and_decision():
    calls = [value_sample_gate(20261103, run_id="value-test", game_id="game-1",
        owner=owner, decision_index=index, probability=.5)
        for owner in (0, 1) for index in range(20)]
    assert calls == [value_sample_gate(20261103, run_id="value-test", game_id="game-1",
        owner=owner, decision_index=index, probability=.5)
        for owner in (0, 1) for index in range(20)]
    assert len({seed for _, seed in calls}) == 40
    assert 0 < sum(selected for selected, _ in calls) < 40
    assert value_sample_gate(20261103, run_id="value-test", game_id="game-1",
        owner=0, decision_index=0, probability=0.)[0] is False


def test_value_training_changes_shared_and_value_parameters_but_not_policy_head():
    torch.manual_seed(11)
    model = TinyValueModel()
    optimizer = create_value_optimizer(model)
    head_before = policy_head_sha(model)
    shared_before = model.shared.weight.detach().clone()
    rows = [ValueRow(f"train-{i // 16}", torch.tensor([[1., .25]]), WLLabel.WIN)
        for i in range(129)]
    validation = [ValueRow(f"validation-{i // 8}", torch.tensor([[1., .25]]),
        WLLabel.WIN) for i in range(32)]
    result = train_value_generation(model, optimizer, rows, validation, seed=7)
    assert result["attempted_steps"] == 2
    assert result["validation_after"]["wl_ce"] < result["validation_before"]["wl_ce"]
    assert result["policy_head_sha"] == policy_head_sha(model) == head_before
    assert all(parameter.grad is None for parameter in model.policy_head.parameters())
    assert not torch.equal(shared_before, model.shared.weight)
    assert len(optimizer.state) > 0


def test_value_metrics_reject_empty_and_report_calibrated_predictions():
    with pytest.raises(ValueError, match="held-out"):
        prediction_metrics([])
    metrics = prediction_metrics([("game-1", 0, (.8, .2)),
        ("game-2", 1, (.2, .8))])
    assert metrics["games"] == 2 and metrics["rows"] == 2
    assert metrics["wl_ce"] == pytest.approx(-torch.tensor(.8).log().item())
    assert metrics["brier"] == pytest.approx(.08)


def _value_episode(tmp_path):
    deck = [{"deckSlot": index, "cardId": card} for index, card in enumerate(HOG_DECK)]
    player = dict(deck=deck, hand=[{**deck[index], "handIndex": index} for index in range(4)],
        cycle=[{**deck[index], "cycleIndex": index - 4} for index in range(4, 8)],
        nextCard={**deck[4], "cycleIndex": 0})
    information = SSPIInformation(0, 100, {"fair": "frame"},
        HandState.from_native(player),
        SSPILegality((), torch.empty((0, 576, 5), dtype=torch.bool)))
    run_id, game_id, model_sha = "value-test", "train-g0001-0001", "model-sha"
    index = next(index for index in range(100)
        if value_sample_gate(20261103, run_id=run_id, game_id=game_id, owner=0,
            decision_index=index, probability=.5)[0])
    _, gate_seed = value_sample_gate(20261103, run_id=run_id, game_id=game_id,
        owner=0, decision_index=index, probability=.5)
    row = dict(**serialize_information(information), decision_index=index,
        value_gate_seed=gate_seed, action="wait", executed_action="wait", wl="WIN")
    identity = dict(run_id=run_id, game_id=game_id, arm=VALUE_ARM, seed=20261103,
        random_stream_id=game_id, collection_protocol="value-only.v2",
        model_sha256_by_owner={"0": model_sha, "1": model_sha},
        value_sample_probability=.5)
    tensor_path = tmp_path / "episode.pt"
    torch.save(dict(schema=VALUE_EPISODE_SCHEMA, identity=identity,
        roots=[], value_rows=[row]), tensor_path)
    action = dict(owner=0, decision_index=index, tick=100,
        fair_sha256=information.sha256, mode="DIRECT", value_selected=True,
        value_gate_seed=gate_seed, action="wait", executed_action="wait")
    action_path = tmp_path / "actions.json"
    action_path.write_text(json.dumps(dict(rows=[action])))
    metadata = dict(schema=VALUE_EPISODE_SCHEMA, status="complete", identity=identity,
        episode_kind="training", training_admissible=True, search_roots=0,
        search_probability=0., value_sample_probability=.5, value_samples=1,
        decisions=dict(decisions={"0": 1, "1": 0}, value_samples={"0": 1, "1": 0}),
        result=dict(labels=["WIN", "LOSS"], winner=0),
        supervision=dict(value_order=["WIN", "LOSS"], eligible=True, excluded_reason=None),
        tensor_sha256=hashlib.sha256(tensor_path.read_bytes()).hexdigest(),
        actions_sha256=hashlib.sha256(action_path.read_bytes()).hexdigest())
    (tmp_path / "episode.json").write_text(json.dumps(metadata))
    return run_id, model_sha, metadata


def test_value_episode_requires_complete_native_and_matching_decision_evidence(tmp_path):
    run_id, model_sha, metadata = _value_episode(tmp_path)
    rows, _ = load_value_episode(tmp_path, expected_kind="training",
        run_id=run_id, model_sha=model_sha)
    assert len(rows) == 1 and rows[0].final_wl == WLLabel.WIN
    metadata["status"] = "quarantined"
    (tmp_path / "episode.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="natural terminal"):
        load_value_episode(tmp_path, expected_kind="training",
            run_id=run_id, model_sha=model_sha)


def test_value_checkpoint_restores_filtered_adamw_state_and_frozen_head(tmp_path):
    model = initialize_fresh_model(20261103)
    optimizer = create_value_optimizer(model)
    head_sha = policy_head_sha(model)
    optimizer.zero_grad(set_to_none=True)
    model.value_head[-1].bias[0].backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    identity = dict(protocol="sspi-value-only-run.v2", run_id="value-test",
        policy_head_sha=head_sha)
    checkpoint = save_checkpoint(tmp_path / "checkpoint.pt", model, optimizer,
        identity=identity, optimizer_steps=1)
    restored = initialize_fresh_model(20261103)
    restored_optimizer = create_value_optimizer(restored)
    result = load_checkpoint(tmp_path / "checkpoint.pt", restored, restored_optimizer,
        expected_identity=identity)
    assert result["file_sha"] == checkpoint["file_sha"]
    assert semantic_digest(restored.state_dict()) == checkpoint["model_sha"]
    assert policy_head_sha(restored) == head_sha
    assert len(restored_optimizer.state) == len(optimizer.state)
