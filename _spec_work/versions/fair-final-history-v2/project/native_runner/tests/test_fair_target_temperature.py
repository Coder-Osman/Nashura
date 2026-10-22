import math

import pytest
import torch

from native_runner.tests.test_fair_search import frame, information, model
from native_runner.tests.test_fair_learner_batch import clone_model, varied_decisions
from native_runner.training.fair_search.consumer import evaluate_decisions
from native_runner.training.fair_search.learner import decision_batch_loss, decode_action, visit_target


def test_targets_are_normalized_immutable_and_hard_ties_match_search():
    visits = {"wait": 4, "play:0:0:1": 20, "play:0:1:2": 8, "play:0:0:2": 0}
    before = dict(visits)
    assert visit_target(visits) == {"wait": .125, "play:0:0:1": .625, "play:0:1:2": .25}
    target = visit_target(visits, .5)
    assert target == pytest.approx({"wait": 16 / 480, "play:0:0:1": 400 / 480, "play:0:1:2": 64 / 480})
    assert visit_target(visits, 0) == {"play:0:0:1": 1.}
    assert visit_target({"wait": 16, "play:0:0:1": 16}, 0) == {"play:0:0:1": 1.}
    assert visit_target(visits, 1e-300) == {"play:0:0:1": 1.}
    assert visits == before


@pytest.mark.parametrize("temperature", [-1., math.inf, math.nan])
def test_invalid_temperatures_are_rejected(temperature):
    with pytest.raises(ValueError, match="temperature"):
        visit_target({"wait": 32}, temperature)


@pytest.mark.parametrize("temperature", [1., .5, 0.])
def test_tempered_loss_matches_full_joint_reference_and_gradient(model, information, temperature):
    rows = varied_decisions(information)
    original_visits = [dict(row.visits) for row in rows]
    reference = clone_model(model)
    actual = decision_batch_loss(model, rows, target_temperature=temperature)[1]
    losses = []
    for row in rows:
        context, _ = reference.encode(row.information.frame)
        for action, weight in visit_target(row.visits, temperature).items():
            result = reference.policy.evaluate_actions(context, decode_action(action, "cpu"),
                                                        **row.information.masks, validate=True)
            losses.append(-result.log_prob[0] * weight / len(rows))
    expected = torch.stack(losses).sum()
    torch.testing.assert_close(actual, expected, atol=2e-6, rtol=2e-5)
    actual.backward()
    expected.backward()
    for (name, parameter), (_, original) in zip(model.named_parameters(), reference.named_parameters()):
        assert (parameter.grad is None) == (original.grad is None), name
        if parameter.grad is not None:
            torch.testing.assert_close(parameter.grad, original.grad, atol=3e-6, rtol=3e-4, msg=name)
    assert [dict(row.visits) for row in rows] == original_visits


def test_validation_reports_hard_target_ce_and_original_soft_ce(model, information):
    rows = varied_decisions(information)
    metrics = evaluate_decisions(model, rows)
    hard = decision_batch_loss(model, rows, target_temperature=0)[1]
    soft = decision_batch_loss(model, rows)[1]
    assert sum(row["top_action_ce"] for row in metrics) / len(rows) == pytest.approx(float(hard.detach()), abs=2e-6)
    assert sum(row["policy_ce"] for row in metrics) / len(rows) == pytest.approx(float(soft.detach()), abs=2e-6)
