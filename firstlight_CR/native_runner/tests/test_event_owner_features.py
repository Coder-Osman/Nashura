"""Event owner neutrality leaves V4 tensors and fixed-step behavior intact."""
from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_training_v4_model import _batch, _model
from native_runner.training.event_model import EventCardPolicyV1, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX


@pytest.mark.parametrize("snapshot_only", [False, True])
def test_absolute_owner_does_not_change_event_context_or_logits(snapshot_only, monkeypatch):
    torch.manual_seed(11)
    backbone = _model()
    backbone.config = replace(backbone.config, snapshot_only=snapshot_only)
    model = EventCardPolicyV1(backbone).eval()
    batch = _batch(1)
    scalars = batch.match_scalars.clone()
    scalars[:, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] = 1
    other = replace(batch, match_scalars=scalars)
    state = model.initial_state(1)
    seen = []
    forward = backbone.forward
    def capture(batch, *args, **kwargs):
        seen.append(batch.match_scalars.clone())
        return forward(batch, *args, **kwargs)
    monkeypatch.setattr(backbone, "forward", capture)
    with torch.no_grad():
        first = model(batch, state)
        second = model(other, state)
    for field in ("gate_logits", "card_logits", "cell_logits", "delay_logits", "policy_context"):
        torch.testing.assert_close(getattr(first, field), getattr(second, field), rtol=0, atol=0)
    assert all(tensor[0, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] == 0 for tensor in seen)
    assert other.match_scalars[0, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] == 1
    assert seen[0].shape == batch.match_scalars.shape
    assert backbone.config.snapshot_only is snapshot_only


def test_direct_v4_still_receives_absolute_owner_feature(monkeypatch):
    backbone = _model().eval()
    batch = _batch(1)
    batch.match_scalars[:, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] = 1
    before_config = backbone.config
    seen = []
    forward = backbone.forward
    def capture(batch, *args, **kwargs):
        seen.append(batch.match_scalars.clone())
        return forward(batch, *args, **kwargs)
    monkeypatch.setattr(backbone, "forward", capture)
    event = EventCardPolicyV1(backbone).eval()
    with torch.no_grad():
        event.encode(batch, event.initial_state(1))
        backbone(batch, backbone.initial_state(1), validate=False)
    assert seen[0][0, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] == 0
    assert seen[1][0, EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] == 1
    assert backbone.config == before_config
