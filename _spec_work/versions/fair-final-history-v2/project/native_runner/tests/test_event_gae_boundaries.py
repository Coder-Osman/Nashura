"""GAE arithmetic and complete-episode boundaries for event training."""

import pytest
import torch

from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.event_model import EventActionV1
from native_runner.training.event_ppo import EventLaneRolloutV1, event_lane_advantages


def _lane(rewards: list[float]) -> EventLaneRolloutV1:
    steps = len(rewards)
    return EventLaneRolloutV1(
        observations=tuple(_batch(1) for _ in rewards),
        actions=EventActionV1(
            act=torch.zeros(steps, dtype=torch.bool),
            card_row=torch.zeros(steps, dtype=torch.long),
            cell=torch.zeros(steps, dtype=torch.long),
            delay_bin=torch.zeros(steps, dtype=torch.long),
        ),
        old_log_prob=torch.zeros(steps),
        reward=torch.tensor(rewards),
        done=torch.tensor([False] * (steps - 1) + [True]),
        truncated=torch.zeros(steps, dtype=torch.bool),
        card_mask=torch.ones(steps, 1, dtype=torch.bool),
        cell_masks=torch.ones(steps, 1, 1, dtype=torch.bool),
        delay_mask=torch.ones(steps, 1, 1, dtype=torch.bool),
        gate_mask=torch.ones(steps, 2, dtype=torch.bool),
        temperatures=(1.0, 1.0, 1.0, 1.0),
    )


@pytest.mark.parametrize("gae_lambda", [0.0, 0.5, 1.0])
def test_perfect_critic_has_zero_advantages(gae_lambda: float) -> None:
    lane = _lane([0.0, 0.0, 4.0])
    values = torch.tensor([1.0, 2.0, 4.0])
    advantages, returns = event_lane_advantages(lane, values, gamma=0.5, gae_lambda=gae_lambda)
    torch.testing.assert_close(advantages, torch.zeros(3))
    torch.testing.assert_close(returns, values)


def test_multistep_gae_matches_hand_calculation() -> None:
    lane = _lane([1.0, -0.5, 2.0])
    values = torch.tensor([0.2, 0.4, 0.8])
    advantages, returns = event_lane_advantages(lane, values, gamma=0.9, gae_lambda=0.8)
    # TD errors = [1.16, -0.18, 1.2]; trace multiplier = 0.72.
    # A2 = 1.2; A1 = -0.18 + 0.72*1.2; A0 = 1.16 + 0.72*A1.
    torch.testing.assert_close(advantages, torch.tensor([1.65248, 0.684, 1.2]))
    torch.testing.assert_close(returns, torch.tensor([1.85248, 1.084, 2.0]))


def test_true_terminal_has_zero_bootstrap() -> None:
    lane = _lane([2.0])
    advantages, returns = event_lane_advantages(
        lane, torch.tensor([7.0]), gamma=0.99, gae_lambda=0.95
    )
    torch.testing.assert_close(advantages, torch.tensor([-5.0]))
    torch.testing.assert_close(returns, torch.tensor([2.0]))


@pytest.mark.parametrize("entrypoint", ["validate", "advantages"])
@pytest.mark.parametrize("boundary", ["interior_truncated", "final_truncated", "incomplete", "interior_done"])
def test_invalid_episode_boundaries_are_rejected_without_mutation(entrypoint: str, boundary: str) -> None:
    lane = _lane([0.0, 0.0, 0.0])
    if boundary == "interior_truncated":
        lane.truncated[1] = True
    elif boundary == "final_truncated":
        lane.done[-1] = False
        lane.truncated[-1] = True
    elif boundary == "incomplete":
        lane.done[-1] = False
    else:
        lane.done[1] = True
    before = [tensor.clone() for tensor in (lane.reward, lane.done, lane.truncated)]
    message = "truncated lanes are unsupported" if "truncated" in boundary else "last step"
    with pytest.raises(ValueError, match=message):
        if entrypoint == "validate":
            lane.validate()
        else:
            event_lane_advantages(lane, torch.ones(3), gamma=0.9, gae_lambda=0.8)
    for actual, expected in zip((lane.reward, lane.done, lane.truncated), before):
        torch.testing.assert_close(actual, expected)
