from dataclasses import replace

import pytest
import torch

from teacher_training import TeacherObjective, teacher_record_loss
from test_teacher_targets import verified_record
from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_ppo import _play_lane
from native_runner.training.event_ppo import EventPpoConfigV1, event_ppo_update


def wait_teacher():
    record = verified_record()
    evidence = {**record.value_evidence, "root_action": "wait"}
    return replace(record, legal_actions=("wait",), visit_counts={"wait": 32},
                   teacher_policy={"wait": 1.}, value_evidence=evidence)


def test_sparse_teacher_ce_keeps_probability_outside_visited_actions():
    model = _event_model().eval()
    record = wait_teacher()
    loss, ce, value, _ = teacher_record_loss(model, record, policy_weight=.5, value_weight=.1)
    # The teacher only visited WAIT, but PLAY still has learner probability.
    # A softmax over visited actions would incorrectly make this CE exactly 0.
    assert ce > 0
    assert float(loss.detach()) == pytest.approx(float(.5 * ce.detach() + .1 * value.detach()))
    loss.backward()
    assert torch.isfinite(model.gate_head[-1].weight.grad).all()
    assert model.gate_head[-1].weight.grad.abs().sum() > 0


def test_teacher_gradients_share_one_ppo_optimizer_step():
    torch.manual_seed(9)
    model = _event_model()
    lanes = [_play_lane(model, 2, rewards=[0., 1.])]
    baseline = _event_model()
    baseline.load_state_dict(model.state_dict())
    config = EventPpoConfigV1(minibatch_lanes=0, epochs=1)
    objective = TeacherObjective([wait_teacher()], policy_weight=.5, value_weight=.1)
    torch.manual_seed(12)
    plain = event_ppo_update(baseline, lanes, torch.optim.SGD(baseline.parameters(), lr=.01), config=config)
    torch.manual_seed(12)
    augmented = event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=.01),
                                 config=config, auxiliary_losses=objective)
    assert plain.optimizer_steps == augmented.optimizer_steps == 1
    assert objective.report()["records"] == objective.report()["batches"] == 1
    assert any(not torch.equal(a, b) for a, b in zip(model.parameters(), baseline.parameters()))


def test_nonfinite_teacher_loss_stops_before_optimizer_update():
    model = _event_model()
    lanes = [_play_lane(model, 1, rewards=[1.])]
    before = [parameter.detach().clone() for parameter in model.parameters()]
    with pytest.raises(ValueError, match="finite scalar"):
        event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=.01),
                         auxiliary_losses=lambda _: [torch.tensor(float("nan"))])
    assert all(torch.equal(a, b) for a, b in zip(before, model.parameters()))


def test_prototype_placeholders_are_rejected_before_training():
    record = replace(wait_teacher(), value_evidence=None, terminal_evidence=None)
    with pytest.raises(ValueError, match="verified"):
        TeacherObjective([record], policy_weight=.5, value_weight=.1)
