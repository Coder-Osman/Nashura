import math

import pytest
import torch

from native_runner.tests.test_fair_search import frame, information, model, ToyBelief, ToyInference
from native_runner.training.fair_search.joint_decode import action_key, install_joint_beam, joint_beam, score_actions
from native_runner.training.fair_search.search import InformationSetSearch
from native_runner.training.fair_search.config import SearchConfig


def test_full_enumeration_normalizes_and_beam_equals_exact_map(model, information):
    context, _ = model.encode(information.frame)
    actions = ["wait", *[f"play:0:{cell}:{delay}" for cell in (0, 1) for delay in (1, 2)]]
    logs = score_actions(model.policy, context, information.masks, actions)
    assert sum(math.exp(value) for value in logs.values()) == pytest.approx(1., abs=2e-6)
    result = joint_beam(model.policy, context, information.masks, width=32)
    assert result["action"] == min(logs, key=lambda key: (-logs[key], key))
    assert result["log_probabilities"] == pytest.approx(logs, abs=2e-6)
    assert all(information.legal(action) for action in result["log_probabilities"])


def test_wait_only_beam(model, information):
    masks = {key: value.clone() for key, value in information.masks.items()}
    masks["gate_mask"][0, 1] = False
    context, _ = model.encode(information.frame)
    result = joint_beam(model.policy, context, masks)
    assert result["action"] == "wait"
    assert result["log_probabilities"]["wait"] == pytest.approx(0.)


def test_deployment_adapter_matches_audit_and_keeps_weights(model, information):
    before = {key: value.clone() for key, value in model.state_dict().items()}
    context, _ = model.encode(information.frame)
    expected = joint_beam(model.policy, context, information.masks)
    uniforms = torch.tensor([[.3, .4, .5, .6]])
    sampled = model.policy.select_action(context, **information.masks, sample=True, sample_uniforms=uniforms)
    install_joint_beam(model.policy)
    actual = model.policy.select_action(context, **information.masks, sample=False)
    assert action_key(actual.action) == expected["action"]
    assert float(actual.log_prob.detach()) == pytest.approx(expected["log_probabilities"][expected["action"]], abs=2e-6)
    again = model.policy.select_action(context, **information.masks, sample=True, sample_uniforms=uniforms)
    assert action_key(again.action) == action_key(sampled.action)
    for key, value in model.state_dict().items():
        torch.testing.assert_close(value, before[key], atol=0, rtol=0)


def test_document_counterexamples():
    first = {"wait": 10, "a1": 8, "a2": 7, "b": 7}
    assert max(first, key=first.get) == "wait"
    assert first["wait"] < sum(value for key, value in first.items() if key != "wait")
    assert first["wait"] ** 2 < sum(value ** 2 for key, value in first.items() if key != "wait")
    second = {"wait": 2, "a1": 8, "a2": 8, "b": 14}
    assert second["a1"] + second["a2"] > second["b"]
    assert max(second, key=second.get) == "b"


def test_perfectly_fitted_visits_greedy_card_differs_from_joint_mode(model, information, monkeypatch):
    masks = {name: mask.clone() for name, mask in information.masks.items()}
    masks["card_mask"][0, 1] = True
    masks["cell_masks"][0, 1, 0] = True
    masks["delay_mask"][0, 1, 1:3] = True
    context, _ = model.encode(information.frame)

    def gate(context, mask):
        return torch.tensor([[2 / 32, 30 / 32]]).log().expand(context.policy_context.shape[0], -1), mask

    def card(context):
        values = torch.full((context.policy_context.shape[0], 8), -1000.)
        values[:, 0] = math.log(16 / 30)
        values[:, 1] = math.log(14 / 30)
        return values

    def cell(context, cards):
        values = torch.full((len(cards), 576), -1000.)
        values[:, :2] = 0.
        return values

    def delay(context, cards, cells):
        values = torch.full((len(cards), 61), -1000.)
        for index, (card_row, position) in enumerate(zip(cards.tolist(), cells.tolist())):
            values[index, 2 if card_row == 0 and position == 1 else 1] = 0.
        return values

    monkeypatch.setattr(model.policy, "event_gate", gate)
    monkeypatch.setattr(model.policy, "card_logits", card)
    monkeypatch.setattr(model.policy, "cell_logits", cell)
    monkeypatch.setattr(model.policy, "delay_logits", delay)
    greedy = action_key(model.policy.select_action(context, **masks, sample=False, validate=True).action)
    beam = joint_beam(model.policy, context, masks, width=32)
    assert greedy.startswith("play:0:")
    assert beam["action"] == "play:1:0:1"
    assert math.exp(beam["log_probabilities"]["play:1:0:1"]) == pytest.approx(14 / 32, abs=1e-6)
    assert sum(math.exp(value) for value in beam["log_probabilities"].values()) == pytest.approx(1., abs=1e-6)


def test_diagnostic_budget_preserves_production_contract(information):
    with pytest.raises(ValueError):
        SearchConfig(simulations=128)
    search = InformationSetSearch(ToyBelief(), ToyInference(), config=SearchConfig(particles=1))
    baseline = search.run(information, seed=12, training=False)
    audited = search.run(information, seed=12, training=False, audit_root=True)
    assert baseline.visits == audited.visits and baseline.selected == audited.selected
    assert not baseline.audit and audited.audit["root_information_sha256"] == information.sha256
    extended = search.run(information, seed=12, training=False, diagnostic_simulations=128, audit_root=True)
    assert sum(extended.visits.values()) == 128
    assert extended.audit["simulations"] == 128
