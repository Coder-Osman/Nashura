from copy import deepcopy
from types import SimpleNamespace

import pytest
import torch

from optimizer_state import (restore_optimizer, optimizer_checkpoint, optimizer_signature,
                             verify_optimizer_signature)


def saved_model():
    model = torch.nn.Linear(2, 1)
    optimizer = torch.optim.AdamW(model.parameters(), lr=7e-5, betas=(.8, .97), weight_decay=.02)
    model(torch.ones(1, 2)).sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    payload = dict(event_ppo_training=optimizer_checkpoint(
        model, optimizer, dict(iterations=1754, optimizer_steps=1754, decision_rows=100),
        SimpleNamespace(optimizer_steps=1, steps=2)))
    return model, payload


def test_resume_preserves_adam_options_moments_and_advances_once():
    model, payload = saved_model()
    before = optimizer_signature(payload)
    old_moments = deepcopy(payload["event_ppo_training"]["optimizer"]["state"])
    optimizer, progress = restore_optimizer(model, payload)
    assert optimizer.param_groups[0]["lr"] == 7e-5
    assert optimizer.param_groups[0]["betas"] == (.8, .97)
    for saved, current in zip(old_moments.values(), optimizer.state.values()):
        assert torch.equal(saved["exp_avg"], current["exp_avg"])
        assert torch.equal(saved["exp_avg_sq"], current["exp_avg_sq"])
    model(torch.ones(1, 2)).sum().backward()
    optimizer.step()
    child = dict(event_ppo_training=optimizer_checkpoint(
        model, optimizer, progress, SimpleNamespace(optimizer_steps=1, steps=3)))
    assert child["event_ppo_training"]["iterations"] == 1756
    assert verify_optimizer_signature(before, optimizer_signature(child), 1) == 2


@pytest.mark.parametrize("damage", ["name", "nan"])
def test_invalid_optimizer_identity_or_moments_stop_resume(damage):
    model, payload = saved_model()
    if damage == "name":
        payload["event_ppo_training"]["parameter_names"].reverse()
    else:
        next(iter(payload["event_ppo_training"]["optimizer"]["state"].values()))["exp_avg"].fill_(float("nan"))
    with pytest.raises(ValueError):
        restore_optimizer(model, payload)
