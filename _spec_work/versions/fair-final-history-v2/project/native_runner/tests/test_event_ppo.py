"""Contract tests for the event-space PPO update."""

from __future__ import annotations

import pytest
import torch

from native_runner.training.event_model import EventActionV1, EventStepV1
from native_runner.training.event_ppo import (
    EventLaneRolloutV1,
    EventPpoConfigV1,
    _slice_action,
    event_lane_advantages,
    event_ppo_update,
)
from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_training_v4_model import _batch

TEMPERATURES = (1.0, 1.0, 1.0, 1.0)


def _masks(model, steps: int) -> tuple[torch.Tensor, torch.Tensor]:
    card_mask = torch.ones(steps, model.card_count, dtype=torch.bool)
    cell_masks = torch.ones(steps, model.card_count, model.cell_count, dtype=torch.bool)
    return card_mask, cell_masks


def _play_lane(
    model,
    steps: int,
    *,
    rewards: list[float] | None = None,
    done_last: bool = True,
    temperatures: tuple[float, float, float, float] = TEMPERATURES,
) -> EventLaneRolloutV1:
    """Walk a lane the way a rollout does: fresh state per decision, sample.

    Every collection path (pretraining batches, evaluation, the renderer) resets
    the recurrent state per decision, so a lane must be collected the same way
    or the update would re-score under a different distribution.
    """

    frames = tuple(_batch(1) for _ in range(steps))
    card_mask, cell_masks = _masks(model, steps)
    delay_mask = torch.ones(steps, model.card_count, model.event_config.delay_bin_count, dtype=torch.bool)
    gate_mask = torch.ones(steps, 2, dtype=torch.bool)
    device = next(model.parameters()).device
    collected: list[EventStepV1] = []
    for index, frame in enumerate(frames):
        batch = frame.to_model_input(device, float_dtype=torch.float32)
        state = model.initial_state(1, device=device)
        context = model.encode(batch, state, episode_start=torch.ones(1, dtype=torch.bool, device=device))
        step = model.select_action(
            context,
            card_mask=card_mask[index : index + 1],
            cell_masks=cell_masks[index : index + 1],
            delay_mask=delay_mask[index : index + 1],
            gate_mask=gate_mask[index : index + 1],
            temperatures=temperatures,
        )
        collected.append(step)

    reward = torch.tensor(rewards if rewards is not None else [0.0] * steps, dtype=torch.float32)
    done = torch.zeros(steps, dtype=torch.bool)
    if done_last:
        done[-1] = True
    return EventLaneRolloutV1(
        observations=frames,
        actions=EventActionV1(
            act=torch.cat([item.action.act for item in collected]),
            card_row=torch.cat([item.action.card_row for item in collected]),
            cell=torch.cat([item.action.cell for item in collected]),
            delay_bin=torch.cat([item.action.delay_bin for item in collected]),
        ),
        old_log_prob=torch.cat([item.log_prob for item in collected]),
        reward=reward,
        done=done,
        truncated=torch.zeros(steps, dtype=torch.bool),
        card_mask=card_mask,
        cell_masks=cell_masks,
        delay_mask=delay_mask,
        gate_mask=gate_mask,
        temperatures=temperatures,
        policy_version="ckpt-test",
    )


def test_advantages_reject_truncation_and_accept_termination() -> None:
    model = _event_model()
    model.eval()
    terminated = _play_lane(model, 3, rewards=[0.0, 0.0, 1.4])
    truncated = _play_lane(model, 3, rewards=[0.0, 0.0, 0.0], done_last=False)
    truncated.truncated[-1] = True

    values = torch.zeros(3)
    terminated_advantage, terminated_return = event_lane_advantages(
        terminated, values, gamma=0.999, gae_lambda=0.95
    )
    with pytest.raises(ValueError, match="truncated lanes are unsupported"):
        event_lane_advantages(truncated, values, gamma=0.999, gae_lambda=0.95)
    assert float(terminated_advantage[-1]) == pytest.approx(1.4, abs=1e-4)
    # With the critic at zero the return is the terminal reward touched only by
    # GAE's own attenuation: advantage carries (gamma*lambda)^2, not gamma^2,
    # which is exactly the bias/variance knob lambda controls.
    assert float(terminated_advantage[0]) == pytest.approx(1.4 * (0.999 * 0.95) ** 2, abs=1e-4)
    assert float(terminated_return[0]) == pytest.approx(float(terminated_advantage[0]), abs=1e-6)


def test_gae_reads_each_steps_next_value_not_zero() -> None:
    """The TD error is ``r + gamma*V(next) - V(now)``, so a step's bootstrap is
    the *next* decision's value.

    Passing zeros there makes every non-terminal delta ``-value``, which turns a
    winning episode's advantage into a large negative number: the update then
    pushes down the decisions that won. Zero values hide it, which is why this
    test uses a constant non-zero critic.
    """

    model = _event_model()
    model.eval()
    lane = _play_lane(model, 3, rewards=[0.0, 0.0, 2.0])
    value = torch.full((3,), 1.0)
    # gamma = lambda = 1 makes the arithmetic exact: with a constant critic the
    # advantage is the terminal reward minus the baseline, at every step.
    advantage, returns = event_lane_advantages(lane, value, gamma=1.0, gae_lambda=1.0)
    assert advantage.tolist() == pytest.approx([1.0, 1.0, 1.0], abs=1e-6)
    assert returns.tolist() == pytest.approx([2.0, 2.0, 2.0], abs=1e-6)

    # Each lane contains exactly one complete episode, never an interior end.
    lane.done[1] = True
    with pytest.raises(ValueError, match="last step"):
        event_lane_advantages(lane, value, gamma=1.0, gae_lambda=1.0)


def test_collecting_policy_has_unit_ratio_and_no_clipping() -> None:
    """PPO's ratio must be exactly 1 on the policy that produced the lane."""

    model = _event_model()
    model.eval()
    lane = _play_lane(model, 4, rewards=[0.0, 0.0, 0.0, 1.4])
    optimizer = torch.optim.SGD(model.parameters(), lr=1e-6)
    config = EventPpoConfigV1(epochs=1, entropy_coefficient=0.0, value_coefficient=0.0)

    stats = event_ppo_update(model, [lane], optimizer, config=config)
    assert stats.ratio_mean == pytest.approx(1.0, abs=1e-4)
    assert stats.clip_fraction == 0.0
    assert stats.approx_kl == pytest.approx(0.0, abs=1e-5)
    assert stats.steps == 4
    assert stats.lanes == 1


def test_target_kl_stops_remaining_batches_and_epochs() -> None:
    from native_runner.training.event_ppo import rollout_sampled_kl
    torch.manual_seed(123)
    model = _event_model()
    model.eval()
    lanes = [_play_lane(model, 1, rewards=[1.4]) for _ in range(2)]
    old = [lane.old_log_prob.detach().clone() for lane in lanes]
    stats = event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=0.05),
        config=EventPpoConfigV1(epochs=3, minibatch_lanes=1, target_kl=1e-12,
            normalize_advantage=False, value_coefficient=0.0, entropy_coefficient=0.0))
    assert stats.optimizer_steps == 1
    assert stats.stopped_early
    assert stats.post_step_kl >= 1e-12
    assert stats.post_step_kl == pytest.approx(rollout_sampled_kl(model, lanes, chunk_steps=32))
    assert not model.training
    for lane, before in zip(lanes, old):
        assert torch.equal(lane.old_log_prob, before)


def test_target_kl_disabled_runs_all_steps() -> None:
    model = _event_model()
    model.eval()
    lanes = [_play_lane(model, 1, rewards=[1.0]) for _ in range(2)]
    stats = event_ppo_update(model, lanes, torch.optim.SGD(model.parameters(), lr=0.0),
                             config=EventPpoConfigV1(epochs=2, minibatch_lanes=1))
    assert stats.optimizer_steps == 4
    assert not stats.stopped_early
    assert stats.post_step_kl is None


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_target_kl_rejects_invalid_threshold(value) -> None:
    with pytest.raises(ValueError, match="target KL"):
        EventPpoConfigV1(target_kl=value)


def test_update_raises_the_probability_of_a_rewarded_action() -> None:
    """The whole point: positive advantage must move the policy toward the
    action that earned it."""

    model = _event_model()
    model.eval()
    lane = _play_lane(model, 1, rewards=[1.4])
    config = EventPpoConfigV1(epochs=1, entropy_coefficient=0.0, value_coefficient=0.0)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.05)

    before = _rescore(model, lane)
    stats = event_ppo_update(model, [lane], optimizer, config=config)
    after = _rescore(model, lane)

    assert stats.advantage_mean > 0.0
    # This also guards a silent no-op: if the behaviour log-prob were left
    # attached to the graph it was collected with, exp(new - old) would
    # differentiate both sides and cancel the gradient to exactly zero.
    assert float(after) > float(before)


def _rescore(model, lane) -> float:
    """Re-score every stored action with the current policy (eval mode).

    One decision at a time with a fresh state, exactly how the lane was
    collected: re-scoring a whole lane in one batch would change the recurrent
    input, and slicing the actions without slicing the frames changes the batch
    size outright.
    """

    was_training = model.training
    model.eval()
    total = 0.0
    try:
        device = next(model.parameters()).device
        with torch.no_grad():
            for index in range(lane.time_steps):
                batch = lane.observations[index].to_model_input(device, float_dtype=torch.float32)
                context = model.encode(batch, model.initial_state(1, device=device),
                                       episode_start=torch.ones(1, dtype=torch.bool, device=device))
                steps = model.evaluate_actions(
                    context,
                    _slice_action(lane.actions, index, index + 1),
                    card_mask=lane.card_mask[index : index + 1],
                    cell_masks=lane.cell_masks[index : index + 1],
                    delay_mask=lane.delay_mask[index : index + 1],
                    gate_mask=lane.gate_mask[index : index + 1],
                    temperatures=lane.temperatures,
                )
                total += float(steps.log_prob.mean())
        return total / lane.time_steps
    finally:
        model.train(was_training)


def test_temperature_mismatch_is_a_hard_error() -> None:
    """The failure that silently ruined an earlier league run: collector and
    update disagreeing about the behaviour temperature."""

    model = _event_model()
    model.eval()
    lane = _play_lane(model, 2, temperatures=(1.0, 2.0, 1.0, 1.0))
    optimizer = torch.optim.SGD(model.parameters(), lr=1e-6)
    with pytest.raises(ValueError, match="temperatures"):
        event_ppo_update(model, [lane], optimizer, expected_temperatures=(1.0, 1.0, 1.0, 1.0))


def test_lane_validation_rejects_inconsistent_rows() -> None:
    model = _event_model()
    model.eval()
    lane = _play_lane(model, 3)

    with pytest.raises(ValueError, match="old_log_prob"):
        EventLaneRolloutV1(
            observations=lane.observations,
            actions=lane.actions,
            old_log_prob=lane.old_log_prob[:2],
            reward=lane.reward,
            done=lane.done,
            truncated=lane.truncated,
            card_mask=lane.card_mask,
            cell_masks=lane.cell_masks,
            delay_mask=lane.delay_mask,
            gate_mask=lane.gate_mask,
            temperatures=lane.temperatures,
        ).validate()

    early = _play_lane(model, 3)
    early.done[0] = True
    with pytest.raises(ValueError, match="last step"):
        early.validate()

    with pytest.raises(ValueError, match="positive"):
        EventPpoConfigV1(clip_ratio=0.0)


def _copy_model(model):
    """A second model with identical weights (deepcopy trips on the catalog)."""

    clone = _event_model()
    clone.load_state_dict(model.state_dict())
    clone.eval()
    return clone


def test_kl_to_reference_is_zero_for_an_identical_reference() -> None:
    model = _event_model()
    model.eval()
    reference = _copy_model(model)
    lane = _play_lane(model, 2, rewards=[0.0, 1.4])
    optimizer = torch.optim.SGD(model.parameters(), lr=1e-6)
    stats = event_ppo_update(
        model,
        [lane],
        optimizer,
        config=EventPpoConfigV1(epochs=1, kl_coefficient=0.05),
        reference_model=reference,
    )
    # Same weights on both sides: the divergence must vanish.
    assert stats.kl_reference == pytest.approx(0.0, abs=1e-6)


def test_kl_to_reference_penalises_and_pulls_back_drift() -> None:
    """A perturbed anchor must register divergence and the penalty must reduce it."""

    torch.manual_seed(13)   # the property depends on the weights, so pin them
    model = _event_model()
    model.eval()
    reference = _copy_model(model)
    with torch.no_grad():
        for parameter in reference.parameters():
            parameter.add_(torch.randn_like(parameter) * 0.05)

    lane = _play_lane(model, 2, rewards=[0.0, 1.4])
    optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
    config = EventPpoConfigV1(epochs=1, kl_coefficient=5.0, entropy_coefficient=0.0, value_coefficient=0.0)

    first = event_ppo_update(model, [lane], optimizer, config=config, reference_model=reference)
    second = event_ppo_update(model, [lane], optimizer, config=config, reference_model=reference)

    assert first.kl_reference > 0.0
    # The penalty dominates at this weight, so the policy moves toward the anchor.
    assert second.kl_reference < first.kl_reference


def test_zero_kl_coefficient_leaves_the_loss_alone() -> None:
    model = _event_model()
    model.eval()
    reference = _copy_model(model)
    with torch.no_grad():
        for parameter in reference.parameters():
            parameter.add_(torch.randn_like(parameter) * 0.05)
    lane = _play_lane(model, 2, rewards=[0.0, 1.4])

    def run(reference_model):
        clone = _copy_model(model)
        optimizer = torch.optim.SGD(clone.parameters(), lr=0.0)
        return event_ppo_update(
            clone,
            [lane],
            optimizer,
            config=EventPpoConfigV1(epochs=1, kl_coefficient=0.0),
            reference_model=reference_model,
        )

    assert run(None).policy_loss == pytest.approx(run(reference).policy_loss, abs=1e-6)


def test_mc_value_targets_are_discounted_terminal_results() -> None:
    from native_runner.training.event_ppo import mc_value_targets

    model = _event_model()
    model.eval()
    lane = _play_lane(model, 3, rewards=[0.0, 0.0, 1.4])
    targets = mc_value_targets(lane, gamma=0.999)
    assert targets.shape == (3,)
    assert float(targets[-1]) == pytest.approx(1.4, abs=1e-6)
    assert float(targets[0]) == pytest.approx(1.4 * 0.999**2, abs=1e-6)


def _rescore_rows(model, lane) -> list[float]:
    return [_rescore_row(model, lane, index) for index in range(lane.time_steps)]


def _rescore_row(model, lane, index: int) -> float:
    device = next(model.parameters()).device
    batch = lane.observations[index].to_model_input(device, float_dtype=torch.float32)
    with torch.no_grad():
        context = model.encode(batch, model.initial_state(1, device=device),
                               episode_start=torch.ones(1, dtype=torch.bool, device=device))
        steps = model.evaluate_actions(
            context,
            _slice_action(lane.actions, index, index + 1),
            card_mask=lane.card_mask[index : index + 1],
            cell_masks=lane.cell_masks[index : index + 1],
            delay_mask=lane.delay_mask[index : index + 1],
            gate_mask=lane.gate_mask[index : index + 1],
            temperatures=lane.temperatures,
        )
    return float(steps.log_prob.mean())


def test_rejection_sampling_immitates_the_kept_episodes_own_actions() -> None:
    """The BC term imitates every decision of a kept episode, weighted by how
    well the episode ended - including the decisions the advantage alone would
    have suppressed."""

    base = _event_model()
    base.eval()
    lane = _play_lane(base, 2, rewards=[0.0, 1.4])

    def run(bc_coefficient: float) -> tuple[list[float], object]:
        model = _copy_model(base)          # every arm starts from the same weights
        optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
        before = _rescore_rows(model, lane)
        stats = event_ppo_update(
            model,
            [lane],
            optimizer,
            config=EventPpoConfigV1(
                epochs=1,
                entropy_coefficient=0.0,
                value_coefficient=0.0,
                bc_coefficient=bc_coefficient,
                bc_margin_threshold=0.0,
            ),
        )
        after = _rescore_rows(model, lane)
        return [new - old for new, old in zip(after, before)], stats

    plain, plain_stats = run(0.0)
    kept, kept_stats = run(1.0)

    # The objective is the episode's margin above the threshold times the mean
    # log-prob of its own actions: a behaviour clone of the kept episode, with the
    # episode result as its weight. The coefficient that scales it lives in the
    # config, so `bc_loss` itself does not carry it.
    mean_log_prob = _rescore(base, lane)
    assert plain_stats.bc_loss == 0.0
    assert kept_stats.bc_loss == pytest.approx(-1.4 * mean_log_prob, rel=1e-4)
    assert run(2.0)[1].bc_loss == pytest.approx(kept_stats.bc_loss, rel=1e-4)
    assert kept_stats.bc_weight_mean == pytest.approx(1.4, abs=1e-5)
    assert kept_stats.bc_kept_fraction == pytest.approx(1.0)

    # Every decision of the kept episode is pushed up, not only the one the
    # advantage already favoured.
    assert kept[0] > plain[0]
    assert kept[1] > plain[1]


def test_rejection_sampling_ignores_losing_episodes() -> None:
    """An episode at or below the threshold contributes nothing, while the PPO
    term is still free to move the same policy."""

    base = _event_model()
    base.eval()
    lane = _play_lane(base, 1, rewards=[-1.4])

    def run(bc_coefficient: float) -> tuple[float, object]:
        model = _copy_model(base)
        optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
        before = _rescore(model, lane)
        stats = event_ppo_update(
            model,
            [lane],
            optimizer,
            config=EventPpoConfigV1(
                epochs=1, entropy_coefficient=0.0, value_coefficient=0.0,
                bc_coefficient=bc_coefficient, bc_margin_threshold=0.0,
            ),
        )
        return _rescore(model, lane) - before, stats

    losing, losing_stats = run(1.0)
    # The update did act on this lane...
    assert losing < 0.0
    # ...but the rejection filter kept nothing: coefficient 1.0 is a no-op here.
    assert losing == pytest.approx(run(0.0)[0], abs=1e-9)
    assert losing_stats.bc_loss == 0.0
    assert losing_stats.bc_kept_fraction == 0.0
    assert losing_stats.bc_weight_mean == 0.0


def test_value_head_parameters_never_move_the_policy() -> None:
    """The warm-up's freeze is a list of modules, so it gets measured, not trusted.

    Perturbing any selected parameter must leave the policy log-prob identical.
    This is what caught ``spatial_encoder.child_value``: it is named for the
    critic but it is the scatter projection that builds the spatial memory the
    cell head reads.
    """

    from native_runner.training.event_ppo import value_head_parameters

    model = _event_model()
    model.eval()
    lane = _play_lane(model, 1, rewards=[1.4])
    reference = _rescore(model, lane)
    selected = value_head_parameters(model)
    names = [name for name, _ in selected]
    assert "backbone.value_head.2.bias" in names, "the critic's output head must be optimised"
    assert not any("child_value" in name for name in names), "the scatter projection builds the cell head's input"

    for name, parameter in selected:
        with torch.no_grad():
            original = parameter.detach().clone()
            parameter.add_(0.05)
        try:
            assert _rescore(model, lane) == reference, f"{name} feeds the policy"
        finally:
            with torch.no_grad():
                parameter.copy_(original)


def test_value_warmup_fits_the_critic_without_moving_the_policy() -> None:
    """The warm-up moves the critic and provably nothing else.

    The synthetic observation fixture is identical for every lane, so no critic
    could tell these episodes apart and only the mechanism is testable here: the
    loss must not rise, and the policy must come out bit-identical. Whether the
    critic actually ranks episodes is measured on real shards, where the
    observations differ; :func:`warm_up_value` reports that as a correlation.
    """

    from native_runner.training.event_ppo import value_head_parameters, warm_up_value

    model = _event_model()
    model.eval()
    outcomes = [1.4, 0.5, -1.4, -0.4]
    lanes = [_play_lane(model, 3, rewards=[0.0, 0.0, outcome]) for outcome in outcomes]
    before = [_rescore(model, lane) for lane in lanes]

    optimizer = torch.optim.Adam([parameter for _, parameter in value_head_parameters(model)], lr=3e-4)
    # Every lane in every step: with four lanes, a two-lane minibatch alternates
    # between fitting a win and fitting a loss and the critic's bias just
    # oscillates around the mean instead of settling on it.
    stats = warm_up_value(model, lanes, optimizer, steps=200, gamma=0.999, chunk_steps=8, minibatch_lanes=4)

    assert stats.lanes == 4
    assert stats.loss_after < stats.loss_before
    # Identical inputs with opposite results: the best fit is the mean, so the
    # critic cannot rank these lanes and the diagnostic must say so rather than
    # report a ranking it does not have.
    assert stats.correlation == pytest.approx(0.0, abs=1e-6)
    assert [_rescore(model, lane) for lane in lanes] == before


class _ValueProbe:
    """A stand-in critic whose reading is a scalar it finds in the observation.

    The diagnostic is how the warm-up is judged, so it gets tested against a
    critic with a known ranking rather than against whatever a random encoder
    happens to produce.
    """

    def __init__(self) -> None:
        self.weight = torch.nn.Parameter(torch.zeros(1))

    def parameters(self):
        return iter([self.weight])

    def initial_state(self, batch_size: int, **_kwargs):
        return None

    def encode(self, batch, _state, **_kwargs):
        reading = batch.candidates.elixir.to(torch.float32) * 0.1
        return type("Context", (), {"value": reading[:, None]})()


def test_value_diagnostics_rank_episodes_by_how_they_ended() -> None:
    import dataclasses

    from native_runner.training.event_ppo import _value_diagnostics

    # Markers give every lane a distinguishable observation, so a critic reading
    # the marker can be right, wrong, or reversed on purpose.
    markers = [14.0, 5.0, -14.0, -4.0]
    outcomes = [1.4, 0.5, -1.4, -0.4]
    lanes = []
    for marker, outcome in zip(markers, outcomes):
        frame = _batch(1)
        candidates = dataclasses.replace(frame.candidates, elixir=torch.tensor([marker]))
        observations = (dataclasses.replace(frame, candidates=candidates),)
        lanes.append(dataclasses.replace(_play_lane(_event_model(), 1, rewards=[outcome]), observations=observations))

    correlation, sign_accuracy = _value_diagnostics(_ValueProbe(), lanes, chunk_steps=8)
    assert correlation == pytest.approx(1.0, abs=1e-6)
    assert sign_accuracy == pytest.approx(1.0)

    flipped = [
        dataclasses.replace(
            lane,
            observations=(
                dataclasses.replace(
                    lane.observations[0],
                    candidates=dataclasses.replace(
                        lane.observations[0].candidates, elixir=-lane.observations[0].candidates.elixir
                    ),
                ),
            ),
        )
        for lane in lanes
    ]
    correlation, sign_accuracy = _value_diagnostics(_ValueProbe(), flipped, chunk_steps=8)
    assert correlation == pytest.approx(-1.0, abs=1e-6)
    assert sign_accuracy == pytest.approx(0.0)


def test_update_reports_how_well_the_critic_ranks_episodes() -> None:
    """The per-iteration statistic that says whether GAE gets a signal: the
    critic's reading of an episode against how the episode ended."""

    model = _event_model()
    model.eval()
    # Same observation, opposite results: the shipped statistic must report that
    # this critic cannot rank them, not that it got half of them right.
    lanes = [_play_lane(model, 2, rewards=[0.0, 1.4]), _play_lane(model, 2, rewards=[0.0, -1.4])]
    optimizer = torch.optim.SGD(model.parameters(), lr=0.0)
    stats = event_ppo_update(
        model, lanes, optimizer, config=EventPpoConfigV1(epochs=1, entropy_coefficient=0.0, value_coefficient=0.0)
    )
    assert stats.value_outcome_correlation == pytest.approx(0.0, abs=1e-6)
    assert stats.value_sign_accuracy == pytest.approx(0.5)
    assert "value_outcome_correlation" in stats.as_dict()
