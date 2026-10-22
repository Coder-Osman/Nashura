"""Packing preserves the full PPO objective and preflight's fail-closed gate."""

from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_ppo import _play_lane
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.training.event_ppo import EventPpoConfigV1, _lane_values, event_ppo_update
from native_runner.training.event_preflight import EventPreflightError, preflight_event_rollout
from native_runner.training.v4.cache import _pad_dynamic_observation


class RecordingAdamW(torch.optim.AdamW):
    def step(self, closure=None):
        self.gradients = [None if p.grad is None else p.grad.detach().clone()
                          for group in self.param_groups for p in group["params"]]
        return super().step(closure)


def clone_model(model):
    clone = _event_model()
    clone.load_state_dict(model.state_dict())
    return clone.train(model.training)


def fixture_lanes():
    torch.manual_seed(714)
    model = _event_model().eval()
    with torch.no_grad():
        lanes = [_play_lane(model, n, rewards=[0.] * (n-1) + [reward], temperatures=temp)
                 for n, reward, temp in [(5, 1.2, (1., 1., 1., 1.)),
                                         (3, -1.1, (1., 1., 1., 1.)),
                                         (2, .4, (.9, 1.1, .8, 1.2))]]
    # Exercise variable padding without changing semantic content.
    frame = lanes[1].observations[0]
    padded = _pad_dynamic_observation(frame, active_effect_count=frame.active_effects.mask.shape[1]+2,
                                     relation_edge_count=frame.relation_edges.mask.shape[1]+3,
                                     candidate_count=frame.candidates.mask.shape[1]+1)
    lanes[1] = replace(lanes[1], observations=(padded, *lanes[1].observations[1:]))
    return model, lanes


@pytest.mark.parametrize("minibatch_lanes", [0, 2])
def test_packing_preserves_gradients_adamw_kl_and_reference_bc(minibatch_lanes):
    original, lanes = fixture_lanes()
    anchor = clone_model(original).eval()
    with torch.no_grad():
        anchor.delay_head[-1].bias.add_(torch.linspace(-.1, .1, anchor.delay_head[-1].bias.numel()))
    results = []
    for packed in (False, True):
        model = clone_model(original)
        optimizer = RecordingAdamW(model.parameters(), lr=1e-5)
        cache = PreparedEventRollout(model, lanes, row_batch_size=4,
                                     minibatch_lanes=minibatch_lanes) if packed else None
        preflight = preflight_event_rollout(model, lanes, prepared=cache, chunk_steps=2)
        stats = event_ppo_update(model, lanes, optimizer, reference_model=anchor, prepared=cache,
                                config=EventPpoConfigV1(minibatch_lanes=minibatch_lanes,
                                    sequence_chunk_steps=2, epochs=2, max_grad_norm=1e6,
                                    bc_coefficient=.2, kl_coefficient=.1, target_kl=1.))
        results.append((model, optimizer, stats, preflight))
        if cache is not None:
            assert cache.reused_values
            assert len(cache.batches) == 3
    before, after = results
    assert before[2].optimizer_steps == after[2].optimizer_steps == (2 if minibatch_lanes == 0 else 4)
    for expected, actual in zip(before[1].gradients, after[1].gradients):
        if expected is None:
            assert actual is None
        else:
            torch.testing.assert_close(actual, expected, atol=3e-5, rtol=3e-4)
    # Fresh AdamW can amplify rounding near zero gradients; gradients above
    # and policy/value/KL metrics below are checked separately.
    for expected, actual in zip(before[0].parameters(), after[0].parameters()):
        torch.testing.assert_close(actual, expected, atol=1e-5, rtol=2e-5)
    for key in ("policy_loss", "value_loss", "entropy", "bc_loss", "bc_kept_fraction",
                "kl_reference", "value_mean", "post_step_kl"):
        assert getattr(after[2], key) == pytest.approx(getattr(before[2], key), abs=2e-5), key
    assert before[3].passed and after[3].passed


def test_cache_packs_once_reuses_values_and_keeps_row_identity(monkeypatch):
    model, lanes = fixture_lanes()
    cache = PreparedEventRollout(model, lanes, row_batch_size=4)
    metadata = [[dict(match_id=f"lane-{i}", observation_tick=j) for j in range(lane.time_steps)]
                for i, lane in enumerate(lanes)]
    with torch.no_grad():
        expected = torch.cat([_lane_values(model, lane, chunk_steps=2) for lane in lanes])
    preflight_event_rollout(model, lanes, prepared=cache, row_metadata=metadata)
    torch.testing.assert_close(cache.take_values(model), expected, atol=2e-6, rtol=2e-5)
    assert cache.take_values(model) is None

    def no_repacking(*args, **kwargs):
        pytest.fail("prepared rollout repacked its immutable observations")
    monkeypatch.setattr("native_runner.training.event_ppo._stack_frames", no_repacking)
    preflight_event_rollout(model, lanes, prepared=cache)
    stats = event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=0), prepared=cache,
                            config=EventPpoConfigV1(minibatch_lanes=0, target_kl=.01))
    assert stats.steps == 10 and cache.reused_values


def test_weight_change_invalidates_values_and_recomputes():
    model, lanes = fixture_lanes()
    cache = PreparedEventRollout(model, lanes, row_batch_size=4)
    preflight_event_rollout(model, lanes, prepared=cache)
    with torch.no_grad():
        model.backbone.value_head[-1].bias.add_(.1)
    assert cache.take_values(model) is None
    # Compare recomputed critic targets to the legacy path after a warmup-like change.
    baseline = clone_model(model)
    config = EventPpoConfigV1(minibatch_lanes=0)
    old = event_ppo_update(baseline, lanes, torch.optim.SGD(baseline.parameters(), lr=0), config=config)
    new = event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=0), config=config, prepared=cache)
    assert not cache.reused_values
    assert new.value_loss == pytest.approx(old.value_loss, abs=2e-5)


def test_failed_preflight_never_publishes_values_and_diagnostics_keep_lane_ids():
    model, lanes = fixture_lanes()
    changed = list(lanes)
    old = lanes[1].old_log_prob.clone()
    old[1] -= .04
    changed[1] = replace(lanes[1], old_log_prob=old)
    cache = PreparedEventRollout(model, changed, row_batch_size=4)
    metadata = [[dict(match_id=f"lane-{i}", observation_tick=j) for j in range(lane.time_steps)]
                for i, lane in enumerate(lanes)]
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, changed, prepared=cache, row_metadata=metadata)
    assert cache.take_values(model) is None
    worst = error.value.metrics["diagnostics"]["joint"]["top_rows"][0]
    assert (worst["lane_index"], worst["row_index"], worst["match_id"]) == (1, 1, "lane-1")
    assert error.value.metrics["rows"] == 10


def test_cache_rejects_changed_behavior_and_optimizer_groups():
    model, lanes = fixture_lanes()
    cache = PreparedEventRollout(model, lanes, row_batch_size=4)
    with pytest.raises(ValueError, match="group boundaries"):
        event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=0), prepared=cache,
                         config=EventPpoConfigV1(minibatch_lanes=1))
    lanes[0].gate_mask[0, 0] = ~lanes[0].gate_mask[0, 0]
    with pytest.raises(ValueError, match="behavior tensors changed"):
        preflight_event_rollout(model, lanes, prepared=cache)


def test_stochastic_training_modules_disable_value_reuse():
    model, lanes = fixture_lanes()
    model.add_module("future_dropout", torch.nn.Dropout(.1))
    cache = PreparedEventRollout(model, lanes, row_batch_size=4)
    preflight_event_rollout(model, lanes, prepared=cache)
    assert cache.take_values(model) is None


def test_packed_update_keeps_full_rollout_kl_early_stop():
    model, lanes = fixture_lanes()
    cache = PreparedEventRollout(model, lanes, row_batch_size=4)
    preflight_event_rollout(model, lanes, prepared=cache)
    stats = event_ppo_update(model, lanes, torch.optim.AdamW(model.parameters(), lr=1e-5),
                            prepared=cache, config=EventPpoConfigV1(
                                minibatch_lanes=0, epochs=3, target_kl=1e-12))
    assert stats.optimizer_steps == 1 and stats.stopped_early
    assert stats.post_step_kl >= 1e-12
