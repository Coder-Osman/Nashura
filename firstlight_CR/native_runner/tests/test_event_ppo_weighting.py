"""Chunking must not change the per-decision PPO objective."""

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_ppo import _play_lane
from native_runner.training.event_ppo import EventPpoConfigV1, event_ppo_update


class RecordingSGD(torch.optim.SGD):
    def step(self, closure=None):
        self.gradients = [None if p.grad is None else p.grad.detach().clone()
                          for group in self.param_groups for p in group['params']]
        return super().step(closure)


def test_uneven_chunks_preserve_gradients_and_metrics():
    torch.manual_seed(71)
    model = _event_model().eval()
    reference = _event_model().eval()
    reference.load_state_dict(model.state_dict())
    with torch.no_grad():
        lanes = [_play_lane(model, 5, rewards=[0, 0, 0, 0, 1.2]),
                 _play_lane(model, 3, rewards=[0, 0, -1.1])]
    results = []
    for chunk in (32, 2, 1):
        candidate = _event_model()
        candidate.load_state_dict(model.state_dict())
        optimizer = RecordingSGD(candidate.parameters(), lr=0.001)
        stats = event_ppo_update(
            candidate, lanes, optimizer,
            config=EventPpoConfigV1(sequence_chunk_steps=chunk, minibatch_lanes=0,
                                    max_grad_norm=1e6, bc_coefficient=0.2,
                                    kl_coefficient=0.1),
            reference_model=reference,
        )
        results.append((optimizer.gradients, stats))
    baseline_grads, baseline_stats = results[0]
    assert baseline_stats.bc_kept_fraction == pytest.approx(5 / 8)
    for gradients, stats in results[1:]:
        for expected, actual in zip(baseline_grads, gradients):
            if expected is None:
                assert actual is None
            else:
                torch.testing.assert_close(actual, expected, atol=3e-5, rtol=3e-4)
        for key in ('policy_loss', 'value_loss', 'entropy', 'ratio_mean',
                    'clip_fraction', 'approx_kl', 'bc_loss', 'bc_kept_fraction',
                    'bc_weight_mean', 'kl_reference', 'value_mean'):
            assert getattr(stats, key) == pytest.approx(getattr(baseline_stats, key), abs=2e-5), key
