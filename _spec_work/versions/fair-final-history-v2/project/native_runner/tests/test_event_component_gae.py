"""Different reward horizons must preserve identity, filtering and cold resume."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import torch

from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_ppo import _play_lane
from native_runner.tests.test_event_optimizer_resume import _assert_tree_equal
from native_runner.tests.test_event_persistent_learner import initial, config, task_for
from native_runner.tests.test_event_tower_rewards import fixture
from native_runner.tests.test_event_overflow import save
from native_runner.training import event_checkpoint
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.training.event_component_gae import (
    COMPONENTS, COMPONENT_KEY, ComponentCritic, ComponentGaeConfig, component_advantages,
    component_rewards, critic_values, resolve_config,
)
from native_runner.training.event_inference import InferenceModel
from native_runner.training.event_ppo import EventPpoConfigV1, event_lane_advantages, event_ppo_update
from native_runner.training.event_preflight import load_complete_rollout, preflight_event_rollout
from native_runner.training.event_rewards import STRONG_TOWER_OVERFLOW_REWARD_CONFIG as V4
from tools import event_rl_update as cli
from tools.event_rl_learner import Learner, CheckpointVerifier, training_metadata


def lane_with_parts(parts):
    parts = torch.tensor(parts, dtype=torch.float32)
    lane = _play_lane(_event_model().eval(), len(parts), rewards=parts.sum(-1).tolist())
    lane.reward_parts = parts
    return lane


@pytest.mark.parametrize("column", range(3))
def test_impulse_horizons_and_no_cross_component_credit(column):
    parts = torch.zeros(12, 3)
    parts[10, column] = -1. if column == 2 else 1.
    lane = lane_with_parts(parts.tolist())
    cfg = ComponentGaeConfig()
    advantage, targets = component_advantages(lane, torch.zeros(12, 3), cfg)
    factor = cfg.gamma * getattr(cfg, COMPONENTS[column]+"_lambda")
    assert advantage[:11, column].tolist() == pytest.approx([
        float(parts[10, column]) * factor**(10-i) for i in range(11)])
    assert torch.count_nonzero(advantage[:, [i for i in range(3) if i != column]]) == 0
    assert torch.count_nonzero(advantage[11]) == 0
    torch.testing.assert_close(targets, advantage)


def test_equal_lambdas_reproduce_scalar_gae_with_nonzero_values():
    lane = lane_with_parts([[0., .2, -.01], [0., -.1, -.02], [1.2, -.0999, -.01]])
    values = torch.tensor([[.5, -.2, -.03], [.6, -.3, -.02], [.9, -.2, -.01]])
    component, targets = component_advantages(lane, values,
        ComponentGaeConfig(outcome_lambda=.95, tower_lambda=.95, overflow_lambda=.95))
    scalar, returns = event_lane_advantages(lane, values.sum(-1), gamma=.999, gae_lambda=.95)
    torch.testing.assert_close(component.sum(-1), scalar)
    torch.testing.assert_close(targets.sum(-1), returns)
    torch.testing.assert_close(targets[-1], lane.reward_parts[-1])


@pytest.mark.parametrize("problem", ["sum", "nan", "shape", "positive_cost", "missing"])
def test_invalid_reward_parts_rejected(problem):
    lane = lane_with_parts([[0., .1, -.02], [1., -.1, 0.]])
    if problem == "sum": lane.reward_parts[0, 0] += 1
    elif problem == "nan": lane.reward_parts[0, 0] = float('nan')
    elif problem == "shape": lane.reward_parts = lane.reward_parts[:, :2]
    elif problem == "positive_cost":
        lane.reward_parts[0, 2] = .02
        lane.reward = lane.reward_parts.sum(-1)
    else:
        lane.reward_parts = None
        lane.reward_config = V4
    with pytest.raises(ValueError):
        component_advantages(lane, torch.zeros(2, 3), ComponentGaeConfig())


def test_effective_parts_exclude_cancelled_rows_and_preserve_reward(tmp_path):
    lanes = []
    for cancelled in (True, False):
        recorder = fixture(config=V4, cancelled=cancelled)
        lane, counts = load_complete_rollout([save(recorder, tmp_path/f'{cancelled}.json')])
        assert counts['excluded_rows'] == (2 if cancelled else 0)
        assert len(lane) == 1 and lane[0].time_steps == 2
        lanes.append(lane[0])
    torch.testing.assert_close(lanes[0].reward_parts, lanes[1].reward_parts)
    assert lanes[0].reward_parts[:, 0].tolist() == pytest.approx([0., 1.1])
    assert lanes[0].reward_parts[:, 2].tolist() == pytest.approx([-.06, -.04])
    a, _ = component_advantages(lanes[0], torch.zeros(2, 3), ComponentGaeConfig())
    b, _ = component_advantages(lanes[1], torch.zeros(2, 3), ComponentGaeConfig())
    torch.testing.assert_close(a, b)


def test_component_heads_preserve_policy_and_rng():
    model = _event_model().eval()
    before, rng = deepcopy(model.state_dict()), torch.get_rng_state().clone()
    critic = ComponentCritic(model)
    _assert_tree_equal(model.state_dict(), before)
    torch.testing.assert_close(torch.get_rng_state(), rng)
    lane = _play_lane(model, 1, rewards=[1.])
    context = model.encode(lane.observations[0], model.initial_state(1),
                           episode_start=torch.ones(1, dtype=torch.bool))
    values = critic_values(context, critic)
    torch.testing.assert_close(values.sum(-1), context.value)
    assert torch.count_nonzero(values[:, 1:]) == 0
    assert torch.count_nonzero(component_rewards(lane)[:, 1:]) == 0


def test_preflight_reuses_component_values_and_invalidates_mutated_parts():
    model = _event_model().eval()
    lane = _play_lane(model, 3, rewards=[0., -.02, 1.])
    lane.reward_parts = torch.tensor([[0., 0., 0.], [0., 0., -.02], [1., 0., 0.]])
    critic = ComponentCritic(model)
    prepared = PreparedEventRollout(model, [lane], row_batch_size=2)
    preflight_event_rollout(model, [lane], prepared=prepared, component_critic=critic)
    stats = event_ppo_update(model, [lane], torch.optim.AdamW(model.parameters(), lr=1e-5),
        config=EventPpoConfigV1(minibatch_lanes=0, component_gae=ComponentGaeConfig()),
        component_critic=critic, component_optimizer=torch.optim.AdamW(critic.parameters(), lr=1e-5),
        prepared=prepared)
    assert prepared.reused_values and stats.optimizer_steps == 1
    assert set(stats.component_stats) == set(COMPONENTS)
    assert stats.value_loss == pytest.approx(sum(s['value_loss'] for s in stats.component_stats.values()))
    lane.reward_parts[0, 1] += .1
    with pytest.raises(ValueError, match='changed'):
        prepared.validate(model, [lane])


def test_component_cache_rejects_auxiliary_parameter_changes():
    model = _event_model().eval()
    lane = _play_lane(model, 2, rewards=[0., 1.])
    critic = ComponentCritic(model)
    prepared = PreparedEventRollout(model, [lane])
    preflight_event_rollout(model, [lane], prepared=prepared, component_critic=critic)
    with torch.no_grad():
        next(critic.parameters()).add_(.1)
    assert prepared.take_values(model, component_critic=critic) is None


def test_packed_and_unpacked_component_objectives_agree():
    torch.manual_seed(29)
    model = _event_model().eval()
    lanes = [_play_lane(model, 3, rewards=[.08, -.02, 1.])]
    lanes[0].reward_parts = torch.tensor([[0., .1, -.02], [0., 0., -.02], [1.1, -.1, 0.]])
    first, second = _event_model().eval(), _event_model().eval()
    first.load_state_dict(model.state_dict())
    second.load_state_dict(model.state_dict())
    heads = ComponentCritic(first)
    heads2 = deepcopy(heads)
    cfg = EventPpoConfigV1(minibatch_lanes=0, component_gae=ComponentGaeConfig())
    a = event_ppo_update(first, lanes, torch.optim.SGD(first.parameters(), lr=1e-5), config=cfg,
        component_critic=heads, component_optimizer=torch.optim.SGD(heads.parameters(), lr=1e-5))
    packed = PreparedEventRollout(second, lanes, row_batch_size=2)
    preflight_event_rollout(second, lanes, prepared=packed, component_critic=heads2)
    b = event_ppo_update(second, lanes, torch.optim.SGD(second.parameters(), lr=1e-5), config=cfg,
        component_critic=heads2, component_optimizer=torch.optim.SGD(heads2.parameters(), lr=1e-5),
        prepared=packed)
    assert a.value_loss == pytest.approx(b.value_loss, abs=1e-6)
    for key, value in first.state_dict().items():
        torch.testing.assert_close(value, second.state_dict()[key], atol=1e-6, rtol=1e-5)
    for key, value in heads.state_dict().items():
        torch.testing.assert_close(value, heads2.state_dict()[key], atol=1e-6, rtol=1e-5)


def test_hot_and_cold_updates_resume_all_critics_and_original_optimizer(initial, tmp_path):
    options = config(initial, tmp_path)
    options['argv'].append('--component-gae')
    worker = Learner(options)
    verifier = CheckpointVerifier(initial)
    checkpoint = initial
    for iteration in (1, 2):
        task = task_for(worker, checkpoint, tmp_path, iteration)
        cold_options = config(checkpoint, tmp_path)
        if iteration == 1: cold_options['argv'].append('--component-gae')
        args = cli.parse_args(cold_options['argv'])
        args.cache_root, args.output = Path(task['cache_root']), tmp_path/f'cold-{iteration}.pt'
        cli.UpdateSession(args).update(args)
        result = worker.update(task)
        verified = verifier.verify({**task, **result})
        assert verified['component_gae']['updates'] == iteration
        hot, cold = event_checkpoint.load_event_checkpoint(task['output']), event_checkpoint.load_event_checkpoint(args.output)
        _assert_tree_equal(hot.model.state_dict(), cold.model.state_dict())
        _assert_tree_equal(hot.payload[cli.PPO_TRAINING_KEY], cold.payload[cli.PPO_TRAINING_KEY])
        _assert_tree_equal(hot.payload[COMPONENT_KEY], cold.payload[COMPONENT_KEY])
        exported, _ = InferenceModel().load(result['descriptor'])
        _assert_tree_equal(exported.state_dict(), hot.model.state_dict())
        with pytest.raises(ValueError, match='mismatch'):
            resolve_config(hot.payload, lambdas=dict(overflow=.9))
        damaged = deepcopy(hot.payload)
        del damaged[COMPONENT_KEY]
        with pytest.raises(ValueError, match='missing'):
            resolve_config(damaged)
        bad_tensor = next(iter(hot.payload[COMPONENT_KEY]['optimizer']['state'].values()))['exp_avg']
        bad_tensor.fill_(float('nan'))
        with pytest.raises(ValueError, match='nonfinite'):
            training_metadata(hot)
        checkpoint = Path(task['output'])


@pytest.mark.parametrize('value', [-.1, 1.1, float('nan'), float('inf')])
def test_invalid_lambda(value):
    with pytest.raises(ValueError): ComponentGaeConfig(overflow_lambda=value)
