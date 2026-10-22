"""PPO update for the event action space.

An event decision is a factorization — ``p(gate) p(card) p(cell|card)
p(delay|card,cell)`` — so a step's behaviour log-prob is the sum of its four head
terms and the ratio PPO clips is ``exp(current - behaviour)`` of that sum. The
per-head terms live in the model (:meth:`EventCardPolicyV1.evaluate_actions`);
this module owns the optimisation: GAE, the clipped surrogate, the value and
entropy terms, and the trust-region bookkeeping.

Observations are re-encoded by the current policy. Decisions are scored as
**independent rows**: the event policy resets its recurrent state per decision
everywhere — pretraining batches, evaluation and the free-running renderer all
call ``initial_state`` afresh with ``episode_start`` set — so carrying a state
here would score the stored actions under a distribution the behaviour policy
never used. Credit still flows along the decision chain through GAE, which is
what the semi-Markov return is for; the model itself has no cross-decision
memory to preserve. Packed batches may span lanes; GAE and optimizer group
boundaries still follow complete lanes. Chunks bound activation memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterator, Sequence

import torch
from torch import Tensor

from .event_batches import PreparedEventRollout, row_batches
from .event_model import EventActionV1, EventCardPolicyV1, EventStepV1
from .event_component_gae import COMPONENTS, ComponentGaeConfig, component_advantages, critic_values
from .v4.cache import _pad_dynamic_observation
from .v4.learning import generalized_advantage_estimate
from .v4.tensors import UniversalSemanticBatchV4, concatenate_tensor_records


@dataclass(frozen=True, slots=True)
class EventPpoConfigV1:
    """Trust region and loss weights for one event PPO iteration."""

    clip_ratio: float = 0.2
    value_coefficient: float = 0.5
    entropy_coefficient: float = 0.01
    gamma: float = 0.999
    gae_lambda: float = 0.95
    epochs: int = 1
    # Lanes per optimizer step. Stepping once per *lane* (the first version) meant
    # 110 updates per iteration, so later lanes were scored by a policy that had
    # already moved and a third of them hit the clip boundary. Standard PPO
    # accumulates a minibatch of lanes and steps once. 0 means "all lanes".
    minibatch_lanes: int = 8
    sequence_chunk_steps: int = 32
    max_grad_norm: float = 1.0
    normalize_advantage: bool = True
    # Legacy anchor support for reproducing old experiments. The current RL
    # entry point disables this term and never loads a reference model.
    kl_coefficient: float = 0.0
    # Rejection sampling, soft form: episodes that ended well pull their own
    # stored actions up. The reward is terminal-only, so "good" is an episode
    # property - weight = how much better than the threshold that episode ended.
    bc_coefficient: float = 0.0
    target_kl: float | None = None
    bc_margin_threshold: float = 0.0
    deck_weights: dict[str, float] | None = None
    reference_deck_id: str | None = None
    component_gae: ComponentGaeConfig | None = None

    def __post_init__(self) -> None:
        if self.component_gae is not None and self.component_gae.gamma != self.gamma:
            raise ValueError("component GAE gamma differs from PPO gamma")
        if self.deck_weights is not None:
            if (not self.deck_weights or any(not math.isfinite(w) or w <= 0 for w in self.deck_weights.values())
                    or not math.isclose(sum(self.deck_weights.values()), 1.) or self.minibatch_lanes != 0):
                raise ValueError("deck weights require positive unit mass and full-rollout accumulation")
        if self.reference_deck_id is not None and (
                self.deck_weights is None or self.reference_deck_id not in self.deck_weights):
            raise ValueError("reference deck must belong to the weighted curriculum")
        if self.target_kl is not None and (not math.isfinite(self.target_kl) or self.target_kl <= 0):
            raise ValueError("target KL must be finite and positive")
        if self.clip_ratio <= 0.0:
            raise ValueError("clip ratio must be positive")
        if self.value_coefficient < 0.0 or self.entropy_coefficient < 0.0:
            raise ValueError("loss weights must be non-negative")
        if not 0.0 <= self.gamma <= 1.0 or not 0.0 <= self.gae_lambda <= 1.0:
            raise ValueError("gamma and gae_lambda must be in [0, 1]")
        if self.epochs < 1 or self.sequence_chunk_steps < 1:
            raise ValueError("epochs and chunk size must be positive")
        if self.minibatch_lanes < 0:
            raise ValueError("minibatch lane count must be non-negative")
        if self.max_grad_norm <= 0.0:
            raise ValueError("max grad norm must be positive")
        if self.kl_coefficient < 0.0:
            raise ValueError("kl coefficient must be non-negative")
        if self.bc_coefficient < 0.0:
            raise ValueError("bc coefficient must be non-negative")


@dataclass(slots=True)
class EventLaneRolloutV1:
    """One match's decisions, in the order the behaviour policy took them.

    ``card_mask`` / ``cell_masks`` are the masks the behaviour policy sampled
    under. They are replayed into the update so the re-scored distribution has
    exactly the support the collected one had: scoring a stored action under a
    wider mask would silently change its probability.
    """

    observations: tuple[UniversalSemanticBatchV4, ...]
    actions: EventActionV1
    old_log_prob: Tensor
    reward: Tensor
    done: Tensor
    truncated: Tensor
    card_mask: Tensor
    cell_masks: Tensor
    # The delay bins the behaviour policy could choose from. Affordability makes
    # this non-trivial: a short bar pushes the earliest deliverable delay up, and
    # re-scoring without that bound would move the action's probability.
    delay_mask: Tensor
    # [T, 2] over (WAIT, ACT): acting is masked off in states where no delay in
    # the vocabulary makes the play deliverable at all.
    gate_mask: Tensor
    temperatures: tuple[float, float, float, float]
    policy_version: str = ""
    # Effective transition duration, including intervening excluded decisions.
    # Gamma/lambda retain the existing per-decision convention.
    decision_ticks: Tensor | None = None
    deck_id: str = ""
    terminal_reward: float | None = None
    reward_config: dict | None = None
    # Outcome, tower shaping, negative overflow cost after cancellation filtering.
    reward_parts: Tensor | None = None

    @property
    def outcome_reward(self) -> float:
        """Unshaped terminal outcome; legacy in-memory callers are terminal-only."""
        return float(self.reward[-1]) if self.terminal_reward is None else self.terminal_reward

    @property
    def time_steps(self) -> int:
        return len(self.observations)

    def validate(self) -> None:
        if self.terminal_reward is not None and not math.isfinite(self.terminal_reward):
            raise ValueError("nonfinite event terminal reward")
        steps = self.time_steps
        ability = self.actions.ability_candidate
        if ability.shape != (steps,) or ability.dtype != torch.long or bool((ability < -1).any()):
            raise ValueError("event ability candidates must be int64 [T], -1 when absent")
        for i in (ability >= 0).nonzero().flatten().tolist():
            from .event_abilities import candidate_ability_mask
            allowed = candidate_ability_mask(self.observations[i])[0]
            if (not bool(self.actions.act[i]) or int(ability[i]) >= allowed.numel()
                    or not bool(allowed[int(ability[i])])
                    or any(int(getattr(self.actions, name)[i]) != -1 for name in ("card_row", "cell"))
                    or not -1 <= int(self.actions.delay_bin[i]) < self.delay_mask.shape[-1]):
                raise ValueError("ability action does not match its native-legal snapshot")
        if self.reward_parts is not None:
            if (self.reward_parts.shape != (steps, 3) or not self.reward_parts.is_floating_point()
                    or not bool(torch.isfinite(self.reward_parts).all())
                    or not torch.allclose(self.reward_parts.sum(-1), self.reward, atol=1e-6, rtol=1e-5)
                    or bool((self.reward_parts[:, 2] > 0).any())):
                raise ValueError("invalid or nonconserving event reward parts")
        if self.decision_ticks is not None:
            if (self.decision_ticks.shape != (steps,) or self.decision_ticks.dtype != torch.long
                    or bool((self.decision_ticks < 0).any())):
                raise ValueError("event lane decision_ticks must be nonnegative int64 [T]")
        if steps == 0:
            raise ValueError("an event lane needs at least one decision")
        expected = {
            "old_log_prob": (steps,),
            "reward": (steps,),
            "done": (steps,),
            "truncated": (steps,),
            "card_mask": (steps, self.card_mask.shape[1]),
            "cell_masks": (steps, self.cell_masks.shape[1], self.cell_masks.shape[2]),
            "delay_mask": (steps, self.card_mask.shape[1], self.delay_mask.shape[2]),
            "gate_mask": (steps, 2),
            "act": (steps,),
            "card_row": (steps,),
            "cell": (steps,),
            "delay_bin": (steps,),
        }
        actual = {
            "old_log_prob": tuple(self.old_log_prob.shape),
            "reward": tuple(self.reward.shape),
            "done": tuple(self.done.shape),
            "truncated": tuple(self.truncated.shape),
            "card_mask": tuple(self.card_mask.shape),
            "cell_masks": tuple(self.cell_masks.shape),
            "delay_mask": tuple(self.delay_mask.shape),
            "gate_mask": tuple(self.gate_mask.shape),
            "act": tuple(self.actions.act.shape),
            "card_row": tuple(self.actions.card_row.shape),
            "cell": tuple(self.actions.cell.shape),
            "delay_bin": tuple(self.actions.delay_bin.shape),
        }
        for name, shape in expected.items():
            if actual[name] != shape:
                raise ValueError(f"event lane {name} has shape {actual[name]}, expected {shape}")
        for frame in self.observations:
            if frame.batch_size != 1:
                raise ValueError("event lane frames must have batch size one")
        if self.done.dtype != torch.bool or self.truncated.dtype != torch.bool:
            raise TypeError("event lane done/truncated must be boolean")
        if bool(self.done[:-1].any()):
            raise ValueError("an event lane may only end the episode on its last step")
        if bool((self.done & self.truncated).any()):
            raise ValueError("an event lane step cannot be both done and truncated")
        if bool(self.truncated.any()):
            raise ValueError("event training requires complete episodes; truncated lanes are unsupported")
        if not bool(self.done[-1]):
            raise ValueError("event training requires a complete episode with done on its last step")


@dataclass(frozen=True, slots=True)
class EventPpoStatsV1:
    policy_loss: float
    value_loss: float
    entropy: float
    ratio_mean: float
    ratio_max: float
    clip_fraction: float
    approx_kl: float
    advantage_mean: float
    value_mean: float
    kl_reference: float
    bc_loss: float
    bc_weight_mean: float
    bc_kept_fraction: float
    value_outcome_correlation: float
    value_sign_accuracy: float
    steps: int
    lanes: int
    optimizer_steps: int = 0
    stopped_early: bool = False
    post_step_kl: float | None = None
    component_stats: dict | None = None

    def as_dict(self) -> dict[str, float | int]:
        return {
            "optimizer_steps": self.optimizer_steps,
            "stopped_early": self.stopped_early,
            "post_step_kl": self.post_step_kl,
            "target_kl_scope": "full_rollout_post_step_k3",
            "policy_loss": self.policy_loss,
            "value_loss": self.value_loss,
            "entropy": self.entropy,
            "ratio_mean": self.ratio_mean,
            "ratio_max": self.ratio_max,
            "clip_fraction": self.clip_fraction,
            "approx_kl": self.approx_kl,
            "advantage_mean": self.advantage_mean,
            "value_mean": self.value_mean,
            "kl_reference": self.kl_reference,
            "bc_loss": self.bc_loss,
            "bc_weight_mean": self.bc_weight_mean,
            "bc_kept_fraction": self.bc_kept_fraction,
            "value_outcome_correlation": self.value_outcome_correlation,
            "value_sign_accuracy": self.value_sign_accuracy,
            "steps": self.steps,
            "lanes": self.lanes,
            "component_stats": self.component_stats,
        }


def _chunk_bounds(steps: int, chunk: int) -> Iterator[tuple[int, int]]:
    for start in range(0, steps, chunk):
        yield start, min(steps, start + chunk)


def _stack_frames(frames: Sequence[UniversalSemanticBatchV4]) -> UniversalSemanticBatchV4:
    """One batch out of per-decision frames, padding dynamic groups to the max.

    The same padding the cache writer applies: padded rows are masked, so the
    extra capacity does not change what the encoder sees.
    """

    active = max(int(frame.active_effects.mask.shape[1]) for frame in frames)
    edges = max(int(frame.relation_edges.mask.shape[1]) for frame in frames)
    candidates = max(int(frame.candidates.mask.shape[1]) for frame in frames)
    packed = concatenate_tensor_records(
        tuple(
            _pad_dynamic_observation(
                frame,
                active_effect_count=active,
                relation_edge_count=edges,
                candidate_count=candidates,
            )
            for frame in frames
        )
    )
    if not isinstance(packed, UniversalSemanticBatchV4):
        raise TypeError("event lane frames have an unexpected record type")
    return packed


def _encode_rows(model: EventCardPolicyV1, frames: Sequence[UniversalSemanticBatchV4]):
    """Encode a chunk of decisions as independent rows with a fresh state."""

    device = next(model.parameters()).device
    batch = _stack_frames(frames).to_model_input(device, float_dtype=torch.float32)
    state = model.initial_state(batch.batch_size, device=device)
    episode_start = torch.ones(batch.batch_size, dtype=torch.bool, device=device)
    return model.encode(batch, state, episode_start=episode_start)


def _lane_values(model: EventCardPolicyV1, lane: EventLaneRolloutV1, *, chunk_steps: int, component_critic=None) -> Tensor:
    """Critic readings for the whole lane, in decision order."""

    readings: list[Tensor] = []
    for start, stop in _chunk_bounds(lane.time_steps, chunk_steps):
        context = _encode_rows(model, lane.observations[start:stop])
        readings.append(critic_values(context, component_critic))
    return torch.cat(readings)


def _lanes_on(lanes: Sequence[EventLaneRolloutV1], device) -> list[EventLaneRolloutV1]:
    """Move a lane's behaviour tensors onto the model's device.

    Frames travel separately (``to_model_input`` handles them per chunk), but the
    actions, log-probs, rewards and masks come straight off disk on the CPU —
    mixing those with CUDA values blows up inside GAE.
    """

    moved: list[EventLaneRolloutV1] = []
    for lane in lanes:
        moved.append(
            EventLaneRolloutV1(
                observations=lane.observations,
                actions=EventActionV1(
                    act=lane.actions.act.to(device),
                    card_row=lane.actions.card_row.to(device),
                    cell=lane.actions.cell.to(device),
                    delay_bin=lane.actions.delay_bin.to(device),
                    ability_candidate=lane.actions.ability_candidate.to(device),
                ),
                old_log_prob=lane.old_log_prob.to(device),
                reward=lane.reward.to(device),
                done=lane.done.to(device),
                truncated=lane.truncated.to(device),
                card_mask=lane.card_mask.to(device),
                cell_masks=lane.cell_masks.to(device),
                delay_mask=lane.delay_mask.to(device),
                gate_mask=lane.gate_mask.to(device),
                temperatures=lane.temperatures,
                policy_version=lane.policy_version,
                deck_id=lane.deck_id,
                terminal_reward=lane.terminal_reward,
                reward_config=lane.reward_config,
                reward_parts=None if lane.reward_parts is None else lane.reward_parts.to(device),
                decision_ticks=None if lane.decision_ticks is None else lane.decision_ticks.to(device),
            )
        )
    return moved


def mc_value_targets(lane: EventLaneRolloutV1, *, gamma: float) -> Tensor:
    """Complete discounted reward sequence, with no terminal-only assumption."""

    from .event_tower_rewards import validate_reward_gamma
    validate_reward_gamma(lane.reward_config, gamma)
    if lane.time_steps == 0:
        raise ValueError("an empty lane has no returns")
    targets = torch.empty_like(lane.reward)
    total = lane.reward.new_zeros(())
    for i in reversed(range(lane.time_steps)):
        total = lane.reward[i].detach() + gamma * total
        targets[i] = total
    return targets


@dataclass(slots=True)
class EventValueWarmupStatsV1:
    loss_before: float
    loss_after: float
    correlation: float
    sign_accuracy: float
    steps: int
    lanes: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "value_loss_before": self.loss_before,
            "value_loss_after": self.loss_after,
            "value_outcome_correlation": self.correlation,
            "value_sign_accuracy": self.sign_accuracy,
            "steps": self.steps,
            "lanes": self.lanes,
        }


_CRITIC_PREFIXES = (
    "backbone.scene_value_skip",
    "backbone.spatial_value_skip",
    "backbone.scalar_value_skip",
    "backbone.event_value_skip",
    "backbone.value_context_norm",
    "backbone.value_head",
)


def value_head_parameters(model: EventCardPolicyV1) -> list[tuple[str, torch.nn.Parameter]]:
    """The critic's own weights.

    Everything that ends at ``value`` is named for it, but the name is not the
    rule: ``backbone.spatial_encoder.child_value`` is the learned scatter
    projection that *builds the spatial memory the cell head reads*, so training
    it would move the policy while looking like critic work. The modules here are
    the ones measured to affect nothing but the value reading; the test suite
    re-measures that rather than trusting this list.
    """

    selected = [
        (name, parameter)
        for name, parameter in model.named_parameters()
        if name.startswith(_CRITIC_PREFIXES)
    ]
    if not selected:
        raise ValueError("model has no critic parameters to warm up")
    return selected


def _value_mse(
    model: EventCardPolicyV1,
    lanes: Sequence[EventLaneRolloutV1],
    *,
    gamma: float,
    chunk_steps: int,
) -> Tensor:
    """Mean squared error of the critic against its Monte-Carlo returns."""

    total = None
    rows = 0
    for lane in lanes:
        values = _lane_values(model, lane, chunk_steps=chunk_steps)
        target = mc_value_targets(lane, gamma=gamma)
        error = (target - values).pow(2).sum()
        total = error if total is None else total + error
        rows += lane.time_steps
    if total is None:
        raise ValueError("value warm-up needs at least one lane")
    return total / max(1, rows)


def _rank_agreement(forecast: Tensor, outcome: Tensor) -> tuple[float, float]:
    """Pearson correlation and sign agreement between readings and results."""

    if forecast.numel() >= 2 and float(forecast.std()) > 0.0 and float(outcome.std()) > 0.0:
        correlation = float(torch.corrcoef(torch.stack((forecast, outcome)))[0, 1])
    else:
        correlation = 0.0
    decided = outcome != 0.0
    if bool(decided.any()):
        sign_accuracy = float((forecast[decided].sign() == outcome[decided].sign()).to(torch.float64).mean())
    else:
        sign_accuracy = 0.0
    return correlation, sign_accuracy


def _value_diagnostics(
    model: EventCardPolicyV1,
    lanes: Sequence[EventLaneRolloutV1],
    *,
    chunk_steps: int,
) -> tuple[float, float]:
    """How well the critic ranks episodes: correlation and sign agreement.

    The value loss alone cannot say whether the critic learned the *ordering* of
    outcomes, which is all GAE uses it for. A critic that predicts every episode
    as a small loss can still have a falling loss while carrying no signal, so
    the correlation between its reading of an episode and how the episode ended
    is the measurement that matters.
    """

    with torch.no_grad():
        predicted: list[float] = []
        actual: list[float] = []
        for lane in lanes:
            values = _lane_values(model, lane, chunk_steps=chunk_steps)
            predicted.append(float(values.mean()))
            actual.append(lane.outcome_reward)
    return _rank_agreement(
        torch.tensor(predicted, dtype=torch.float64), torch.tensor(actual, dtype=torch.float64)
    )


def warm_up_value(
    model: EventCardPolicyV1,
    lanes: Sequence[EventLaneRolloutV1],
    optimizer: torch.optim.Optimizer,
    *,
    steps: int,
    gamma: float,
    chunk_steps: int,
    minibatch_lanes: int = 8,
    max_grad_norm: float = 1.0,
) -> EventValueWarmupStatsV1:
    """Regress the critic onto the episodes' own results before PPO starts.

    At the beginning of RL the critic is whatever imitation left behind, and GAE
    computed from a critic with no idea which episodes went well is close to
    noise. Fitting it to the returns first, with the policy frozen, gives the
    first PPO iterations an advantage signal that means something.

    ``optimizer`` must be built over :func:`value_head_parameters`; the policy
    cannot move here even if the loss wanted it to.
    """

    if steps <= 0:
        raise ValueError("value warm-up needs a positive step count")
    if not lanes:
        raise ValueError("value warm-up needs at least one lane")
    lanes = _lanes_on(lanes, next(model.parameters()).device)
    for lane in lanes:
        lane.validate()

    was_training = model.training
    model.train()
    try:
        with torch.no_grad():
            loss_before = float(_value_mse(model, lanes, gamma=gamma, chunk_steps=chunk_steps))
        batch = max(1, minibatch_lanes)
        cursor = 0
        for _step in range(steps):
            subset = [lanes[(cursor + index) % len(lanes)] for index in range(batch)]
            cursor += batch
            optimizer.zero_grad(set_to_none=True)
            loss = _value_mse(model, subset, gamma=gamma, chunk_steps=chunk_steps)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                [parameter for _, parameter in value_head_parameters(model)], max_grad_norm
            )
            optimizer.step()
        with torch.no_grad():
            loss_after = float(_value_mse(model, lanes, gamma=gamma, chunk_steps=chunk_steps))
        correlation, sign_accuracy = _value_diagnostics(model, lanes, chunk_steps=chunk_steps)
        return EventValueWarmupStatsV1(
            loss_before=loss_before,
            loss_after=loss_after,
            correlation=correlation,
            sign_accuracy=sign_accuracy,
            steps=steps,
            lanes=len(lanes),
        )
    finally:
        model.train(was_training)


def _single_head_kl(current: Tensor, reference: Tensor) -> Tensor:
    """KL(current ‖ reference) per row for one head.

    Masked vocabulary entries carry -inf on both sides, so their difference is
    nan; those entries also have zero probability, so zeroing the difference is
    both correct and what keeps the sum finite.
    """

    difference = current - reference
    difference = torch.where(torch.isfinite(difference), difference, torch.zeros_like(difference))
    return (current.exp() * difference).sum(dim=-1)


def _head_kl(
    current: tuple[Tensor, Tensor, Tensor, Tensor],
    reference: tuple[Tensor, Tensor, Tensor, Tensor],
    *,
    active: Tensor,
    ability_active: Tensor | None = None,
    ability_delay: bool = False,
) -> Tensor:
    """KL summed over the four heads; only the gate applies to WAIT rows."""

    if len(current) != len(reference):
        raise ValueError("reference policy must share the event action contract")
    ability_active = torch.zeros_like(active) if ability_active is None else ability_active
    gate_kl = _single_head_kl(current[0], reference[0])
    conditional = sum(_single_head_kl(cur, ref) for cur, ref in zip(current[1:4], reference[1:4]))
    result = gate_kl + conditional * (active - ability_active)
    if len(current) == 5:
        result = result + _single_head_kl(current[4], reference[4]) * ability_active
        if ability_delay:
            result = result + _single_head_kl(current[3], reference[3]) * ability_active
    return result


def _slice_action(action: EventActionV1, start: int, stop: int) -> EventActionV1:
    return EventActionV1(
        act=action.act[start:stop],
        card_row=action.card_row[start:stop],
        cell=action.cell[start:stop],
        delay_bin=action.delay_bin[start:stop],
        ability_candidate=action.ability_candidate[start:stop],
    )


def event_lane_advantages(
    lane: EventLaneRolloutV1,
    values: Tensor,
    *,
    gamma: float,
    gae_lambda: float,
) -> tuple[Tensor, Tensor]:
    """GAE for one lane, with the next decision's value as each step's bootstrap.

    ``next_values[i]`` is the critic's reading of the state decision ``i`` leads
    to, which the reward is defined against: without it the TD error of a step
    becomes ``-value[i]`` and a *winning* episode accumulates a large negative
    advantage, so PPO learns to push down the very decisions that won.  The
    final step bootstraps from zero at true termination. Only complete episodes
    are supported: truncated/incomplete lanes have no successor observation for
    a valid bootstrap and must not be assigned a fabricated terminal reward.
    """

    lane.validate()
    from .event_tower_rewards import validate_reward_gamma
    validate_reward_gamma(lane.reward_config, gamma)
    if tuple(values.shape) != (lane.time_steps,):
        raise ValueError("event lane values must align with its steps")
    targets = values.unsqueeze(-1)
    next_values = torch.zeros_like(targets)
    if lane.time_steps > 1:
        # A step that terminated has no next state; the rest read the value of the
        # decision that followed them.
        next_values[:-1] = torch.where(
            lane.done[:-1].unsqueeze(-1), torch.zeros_like(targets[1:]), targets[1:]
        )
    advantages, returns = generalized_advantage_estimate(
        lane.reward.detach().unsqueeze(-1),
        targets,
        next_values,
        lane.done.unsqueeze(-1),
        lane.truncated.unsqueeze(-1),
        gamma=gamma,
        gae_lambda=gae_lambda,
    )
    return advantages.squeeze(-1), returns.squeeze(-1)


@torch.no_grad()
def objective_row_weights(lanes, deck_weights, *, device):
    """Each deck contributes its requested mass, independent of lane lengths."""
    rows = sum(lane.time_steps for lane in lanes)
    if deck_weights is None:
        return torch.full((rows,), 1. / rows, device=device)
    counts = {deck: sum(l.time_steps for l in lanes if l.deck_id == deck) for deck in deck_weights}
    if any(n == 0 for n in counts.values()) or any(l.deck_id not in counts for l in lanes):
        raise ValueError("weighted PPO requires valid rows for every configured deck")
    return torch.cat([torch.full((l.time_steps,), deck_weights[l.deck_id] / counts[l.deck_id],
                                 device=device) for l in lanes])


def normalize_deck_advantages(advantage, lanes, deck_weights):
    groups = (tuple(deck_weights) if deck_weights is not None else (None,))
    result = advantage.clone()
    for deck in groups:
        selected = torch.cat([torch.full((l.time_steps,), deck is None or l.deck_id == deck,
                                         dtype=torch.bool, device=advantage.device) for l in lanes])
        values = advantage[selected]
        if values.numel() > 1:
            result[selected] = (values - values.mean()) / (values.std(unbiased=False) + 1e-8)
    return result


@torch.no_grad()
def rollout_sampled_kl(model, lanes, *, chunk_steps: int,
                       prepared: PreparedEventRollout | None = None, deck_weights=None) -> float:
    """Row-weighted k3 estimate on the immutable full behavior rollout.

    A stopping signal, not a hard KL bound: the crossing step is retained.
    Nonfinite scores fail closed by stopping any subsequent optimizer steps.
    """
    was_training = model.training
    model.eval()
    total, rows = None, 0
    try:
        if prepared is not None:
            prepared.validate(model, lanes)
        batches = prepared.batches if prepared is not None else row_batches(
            _lanes_on(lanes, next(model.parameters()).device), chunk_steps=chunk_steps)
        weights = objective_row_weights(lanes, deck_weights, device=next(model.parameters()).device)
        for batch in batches:
            scored = batch.evaluate(model, batch.encode(model))
            delta = scored.log_prob.double() - batch.old_log_prob.double()
            estimate = delta.expm1() - delta
            subtotal = (estimate * batch.select(weights)).sum()
            total = subtotal if total is None else total + subtotal
            rows += batch.stop - batch.start
        result = float(total)
        return result if math.isfinite(result) else math.inf
    finally:
        model.train(was_training)


def event_ppo_update(
    model: EventCardPolicyV1,
    lanes: Sequence[EventLaneRolloutV1],
    optimizer: torch.optim.Optimizer,
    *,
    config: EventPpoConfigV1 | None = None,
    expected_temperatures: tuple[float, float, float, float] | None = None,
    grad_scaler: "torch.amp.GradScaler | None" = None,
    reference_model: EventCardPolicyV1 | None = None,
    prepared: PreparedEventRollout | None = None,
    component_critic=None,
    component_optimizer=None,
    phase_timings: dict | None = None,
) -> EventPpoStatsV1:
    """Run one PPO iteration over collected lanes.

    ``reference_model`` is the frozen IL anchor: with ``config.kl_coefficient``
    above zero its distribution is added as a per-head KL penalty, which is what
    keeps optimisation from eroding the pretrained behaviour.

    Values for the advantages come from the unchanged preflight critic when
    available, otherwise they are re-read before the epochs. Shipped rollout
    rows do not need a value column.
    ``expected_temperatures`` is a tripwire for the failure that silently ruined
    an earlier league run: if the collector and the update disagree about the
    behaviour temperature, every ratio compares two different distributions.
    """

    actual = config or EventPpoConfigV1()
    split = actual.component_gae is not None
    if split != (component_critic is not None) or split != (component_optimizer is not None):
        raise ValueError("component GAE requires its critic and optimizer together")
    if split and grad_scaler is not None:
        raise ValueError("component GAE currently requires unscaled FP32 optimizer steps")
    if not lanes:
        raise ValueError("event PPO update needs at least one lane")
    source_lanes = lanes
    if prepared is not None:
        prepared.validate(model, lanes, minibatch_lanes=actual.minibatch_lanes)
    lanes = prepared.lanes if prepared is not None else _lanes_on(lanes, next(model.parameters()).device)
    for lane in lanes:
        lane.validate()
        if expected_temperatures is not None and tuple(lane.temperatures) != tuple(expected_temperatures):
            raise ValueError(
                f"rollout temperatures {tuple(lane.temperatures)} do not match "
                f"the expected {tuple(expected_temperatures)}"
            )

    weights = objective_row_weights(lanes, actual.deck_weights, device=next(model.parameters()).device)
    reference_rows = sum(l.time_steps for l in lanes if l.deck_id == actual.reference_deck_id)
    if actual.reference_deck_id is not None and reference_rows == 0:
        raise ValueError("reference deck has no valid decisions")

    from .event_timing import DeviceTimings
    timer = DeviceTimings(next(model.parameters()).device)
    was_training = model.training
    model.train()
    try:
        # ---- pre-pass: critic readings, then advantages on a common scale
        advantages: list[Tensor] = []
        returns: list[Tensor] = []
        value_means: list[float] = []
        component_readings, component_signals = [], []
        with torch.no_grad():
            cached_values = prepared.take_values(model, component_critic=component_critic) if prepared is not None else None
            if prepared is not None and cached_values is None:
                cached_values = prepared.restore_order([critic_values(batch.encode(model), component_critic)
                                           for batch in prepared.batches])
            lane_values = iter(cached_values.split([lane.time_steps for lane in lanes])) if cached_values is not None else None
            for lane in lanes:
                values = next(lane_values) if lane_values is not None else _lane_values(
                    model, lane, chunk_steps=actual.sequence_chunk_steps, component_critic=component_critic)
                if split:
                    signals, lane_return = component_advantages(lane, values, actual.component_gae)
                    lane_advantage = signals.sum(-1)
                    component_readings.append(values)
                    component_signals.append(signals)
                else:
                    lane_advantage, lane_return = event_lane_advantages(
                        lane, values, gamma=actual.gamma, gae_lambda=actual.gae_lambda)
                advantages.append(lane_advantage)
                returns.append(lane_return)
                value_means.append(float((values[:, 0] if split else values).mean()))
        # The critic's reading of each episode against how it actually ended: the
        # statistic that says whether GAE is being fed a signal or noise, which
        # the value loss alone cannot show.
        value_rank, value_sign = _rank_agreement(
            torch.tensor(value_means, dtype=torch.float64),
            torch.tensor([lane.outcome_reward for lane in lanes], dtype=torch.float64),
        )
        flat_advantage = torch.cat(advantages)
        if actual.normalize_advantage and flat_advantage.numel() > 1:
            # Same convention as the V4 update: center and scale together, which
            # also makes the reported advantage mean a check that it happened.
            flat_advantage = normalize_deck_advantages(flat_advantage, lanes, actual.deck_weights)

        totals = {
            "policy_loss": 0.0,
            "kl_reference": 0.0,
            "bc_loss": 0.0,
            "bc_kept": 0,
            "bc_total": 0,
            "bc_weight": 0.0,
            "value_loss": 0.0,
            "entropy": 0.0,
            "ratio_sum": 0.0,
            "ratio_max": 0.0,
            "clipped": 0.0,
            "kl": 0.0,
            "steps": 0,
        }
        totals = {key: torch.zeros((), dtype=torch.float64, device=flat_advantage.device)
                  for key in totals}
        cursor = sum(lane.time_steps for lane in lanes)

        flat_return = torch.cat(returns)
        optimizer_steps, stopped_early, post_step_kl = 0, False, None
        component_loss = torch.zeros(3, device=flat_return.device, dtype=torch.float64)
        batch_lanes = actual.minibatch_lanes if actual.minibatch_lanes > 0 else len(lanes)
        for _epoch in range(actual.epochs):
            for group_start in range(0, len(lanes), batch_lanes):
                optimizer.zero_grad(set_to_none=True)
                if split:
                    component_optimizer.zero_grad(set_to_none=True)
                group_stop = min(len(lanes), group_start + batch_lanes)
                group_rows = sum(lane.time_steps for lane in lanes[group_start:group_stop])
                batches = prepared.groups[group_start] if prepared is not None else row_batches(
                    lanes, chunk_steps=actual.sequence_chunk_steps,
                    lane_start=group_start, lane_stop=group_stop)
                for batch in batches:
                    timer.start("forward_loss")
                    context = batch.encode(model)
                    steps = batch.evaluate(model, context)
                    # Behaviour-side values are data, not a differentiable path.
                    # If old_log_prob still carried the graph it was collected
                    # with (same-process rollouts), exp(new - old) would
                    # differentiate both sides and cancel to a zero gradient.
                    old_log_prob = batch.old_log_prob
                    advantage = batch.select(flat_advantage).detach()
                    target = batch.select(flat_return).detach()

                    ratio = torch.exp(steps.log_prob - old_log_prob)
                    row_scale = (batch.select(weights) * flat_advantage.numel()
                                 if actual.deck_weights is not None else torch.ones_like(ratio))
                    unclipped = ratio * advantage
                    clipped = torch.clamp(ratio, 1.0 - actual.clip_ratio, 1.0 + actual.clip_ratio) * advantage
                    policy_loss = -(torch.minimum(unclipped, clipped) * row_scale).mean()
                    value = critic_values(context, component_critic)
                    if split:
                        per_component_loss = .5 * ((target-value).square() * row_scale[:, None]).mean(0)
                        value_loss = per_component_loss.sum()
                    else:
                        value_loss = 0.5 * ((target - value).pow(2) * row_scale).mean()
                    entropy = (_total_entropy(steps) * row_scale).mean()
                    bc_term = None
                    if actual.bc_coefficient > 0.0:
                        # Weight the episode's own actions by how well it ended.
                        episode_weight = torch.clamp(
                            batch.episode_reward - actual.bc_margin_threshold, min=0.0
                        )
                        bc_term = -(episode_weight * steps.log_prob * row_scale).mean()
                    kl_reference = None
                    if reference_model is not None and actual.kl_coefficient > 0.0:
                        with torch.no_grad():
                            reference_context = batch.encode(reference_model)
                            reference_log_probs = reference_model.head_log_probs(
                                reference_context,
                                card_row=batch.actions.card_row,
                                cell=batch.actions.cell,
                                ability_candidate=batch.actions.ability_candidate,
                                gate_mask=batch.gate_mask,
                                card_mask=batch.card_mask,
                                cell_masks=batch.cell_masks,
                                delay_mask=batch.delay_mask,
                                temperatures=batch.temperatures,
                            )
                        current_log_probs = model.head_log_probs(
                            context,
                            card_row=batch.actions.card_row,
                            cell=batch.actions.cell,
                            ability_candidate=batch.actions.ability_candidate,
                            gate_mask=batch.gate_mask,
                            card_mask=batch.card_mask,
                            cell_masks=batch.cell_masks,
                            delay_mask=batch.delay_mask,
                            temperatures=batch.temperatures,
                        )
                        kl_reference = _head_kl(
                            current_log_probs, reference_log_probs, active=steps.action.act.to(torch.float32),
                            ability_active=(steps.action.ability_candidate >= 0).to(torch.float32),
                            ability_delay=model.config.ability_delay,
                        )
                        if actual.reference_deck_id is not None:
                            reference_scale = torch.tensor(
                                [deck == actual.reference_deck_id for deck in batch.deck_ids],
                                device=kl_reference.device, dtype=kl_reference.dtype)
                            kl_reference = kl_reference * reference_scale * (flat_advantage.numel() / reference_rows)
                    loss = (
                        policy_loss
                        + actual.value_coefficient * value_loss
                        - actual.entropy_coefficient * entropy
                    )
                    if kl_reference is not None:
                        loss = loss + actual.kl_coefficient * kl_reference.mean()
                    if bc_term is not None:
                        loss = loss + actual.bc_coefficient * bc_term

                    # Weight chunk means by rows so padding/chunk boundaries
                    # do not change the minibatch objective.
                    chunk_rows = batch.stop - batch.start
                    loss = loss * (chunk_rows / group_rows)
                    scaled = grad_scaler.scale(loss) if grad_scaler is not None else loss
                    timer.stop("forward_loss")
                    timer.start("backward")
                    scaled.backward()
                    timer.stop("backward")

                    with torch.no_grad():
                        totals["policy_loss"] += policy_loss.double() * chunk_rows
                        totals["value_loss"] += value_loss.double() * chunk_rows
                        if split:
                            component_loss += per_component_loss.detach().double() * chunk_rows
                        totals["entropy"] += entropy.double() * chunk_rows
                        totals["ratio_sum"] += ratio.sum().double()
                        totals["ratio_max"] = torch.maximum(totals["ratio_max"], ratio.max().double())
                        totals["clipped"] += (
                            (torch.abs(ratio - 1.0) > actual.clip_ratio).sum()
                        )
                        totals["kl"] += (old_log_prob - steps.log_prob).sum().double()
                        if kl_reference is not None:
                            totals["kl_reference"] += kl_reference.mean().double() * chunk_rows
                        if bc_term is not None:
                            totals["bc_loss"] += bc_term.double() * chunk_rows
                        episode_weight = (batch.episode_reward - actual.bc_margin_threshold).clamp_min(0)
                        totals["bc_kept"] += (episode_weight > 0).sum()
                        totals["bc_weight"] += episode_weight.sum().double()
                        totals["bc_total"] += chunk_rows
                        totals["steps"] += chunk_rows

                timer.start("optimizer")
                if grad_scaler is None:
                    parameters = list(model.parameters()) + ([] if not split else list(component_critic.parameters()))
                    torch.nn.utils.clip_grad_norm_(parameters, actual.max_grad_norm, error_if_nonfinite=split)
                    optimizer.step()
                    if split:
                        component_optimizer.step()
                else:
                    grad_scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), actual.max_grad_norm)
                    grad_scaler.step(optimizer)
                    grad_scaler.update()
                optimizer.zero_grad(set_to_none=True)
                if split:
                    component_optimizer.zero_grad(set_to_none=True)
                timer.stop("optimizer")
                optimizer_steps += 1
                if actual.target_kl is not None:
                    post_step_kl = rollout_sampled_kl(
                        model, source_lanes, chunk_steps=actual.sequence_chunk_steps, prepared=prepared,
                        deck_weights=actual.deck_weights,
                    )
                    if not math.isfinite(post_step_kl) or post_step_kl >= actual.target_kl:
                        stopped_early = True
                        break
            if stopped_early:
                break

        totals = dict(zip(totals, torch.stack(list(totals.values())).cpu().tolist()))
        scored_rows = max(1, int(totals["steps"]))
        component_stats = None
        if split:
            readings, signals = torch.cat(component_readings), torch.cat(component_signals)
            component_stats = {name: dict(
                gae_lambda=getattr(actual.component_gae, name+"_lambda"),
                value_mean=float((readings[:, i]*weights).sum()),
                advantage_mean=float((signals[:, i]*weights).sum()),
                advantage_abs_mean=float((signals[:, i].abs()*weights).sum()),
                value_loss=float(component_loss[i])/scored_rows,
            ) for i, name in enumerate(COMPONENTS)}
        if phase_timings is not None:
            phase_timings.update(timer.result())
        return EventPpoStatsV1(
            component_stats=component_stats,
            optimizer_steps=optimizer_steps, stopped_early=stopped_early, post_step_kl=post_step_kl,
            policy_loss=totals["policy_loss"] / scored_rows,
            value_loss=totals["value_loss"] / scored_rows,
            entropy=totals["entropy"] / scored_rows,
            ratio_mean=totals["ratio_sum"] / scored_rows,
            ratio_max=totals["ratio_max"],
            clip_fraction=totals["clipped"] / scored_rows,
            approx_kl=totals["kl"] / scored_rows,
            advantage_mean=float(flat_advantage.mean()),
            value_mean=sum(value * lane.time_steps for value, lane in zip(value_means, lanes)) / cursor,
            kl_reference=totals["kl_reference"] / scored_rows,
            bc_loss=totals["bc_loss"] / scored_rows,
            bc_weight_mean=totals["bc_weight"] / max(1, totals["bc_kept"]),
            bc_kept_fraction=totals["bc_kept"] / max(1, totals["bc_total"]),
            value_outcome_correlation=value_rank,
            value_sign_accuracy=value_sign,
            steps=sum(lane.time_steps for lane in lanes),
            lanes=len(lanes),
        )
    finally:
        model.train(was_training)


def _total_entropy(steps: EventStepV1) -> Tensor:
    return steps.gate_entropy + steps.card_entropy + steps.cell_entropy + steps.delay_entropy + steps.ability_entropy
