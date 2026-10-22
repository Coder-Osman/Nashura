"""Separate credit horizons, with learner-only tower and overflow critics.

The original critic becomes the outcome critic. Auxiliary heads start at zero,
so the sum initially equals the old prediction; this preserves its warm start,
not a claim that a shaped critic already estimates outcome alone. All heads
learn their own lambda returns, using one shared observation encoding.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import math

import torch
from torch import nn

from .v4.learning import generalized_advantage_estimate

COMPONENTS = ("outcome", "tower", "overflow")
COMPONENT_KEY = "event_component_gae"


@dataclass(frozen=True)
class ComponentGaeConfig:
    gamma: float = .999
    outcome_lambda: float = .99
    tower_lambda: float = .90
    overflow_lambda: float = .50

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"invalid component GAE {name}")

    def as_dict(self):
        return dict(version="event-component-gae.v1", components=list(COMPONENTS),
                    credit="executed-act-or-wait.v1", **asdict(self))

    @classmethod
    def from_dict(cls, value):
        config = cls(**{name: value[name] for name in cls.__dataclass_fields__})
        if config.as_dict() != value:
            raise ValueError("unsupported component GAE configuration")
        return config


class ComponentCritic(nn.Module):
    def __init__(self, model):
        super().__init__()
        # Copying avoids consuming RNG or changing the policy / original AdamW.
        self.heads = nn.ModuleDict({name: deepcopy(model.backbone.value_head)
                                   for name in COMPONENTS[1:]})
        for head in self.heads.values():
            nn.init.zeros_(head[-1].weight)
            nn.init.zeros_(head[-1].bias)

    def forward(self, context):
        return torch.stack([self.heads[name](context).squeeze(-1)
                            for name in COMPONENTS[1:]], dim=-1)


def critic_values(context, critic=None):
    outcome = context.value.reshape(-1)
    if critic is None:
        return outcome
    return torch.cat((outcome[:, None], critic(context.value_context)), dim=-1)


def component_rewards(lane):
    if lane.reward_parts is not None:
        return lane.reward_parts
    if lane.reward_config is not None:
        raise ValueError("component GAE needs recorded reward parts for shaped lanes")
    # Historical terminal-only data has no invented shaping / overflow evidence.
    return torch.stack((lane.reward, torch.zeros_like(lane.reward),
                        torch.zeros_like(lane.reward)), dim=-1)


def component_advantages(lane, values, config):
    lane.validate()
    from .event_tower_rewards import validate_reward_gamma
    validate_reward_gamma(lane.reward_config, config.gamma)
    if values.shape != (lane.time_steps, 3):
        raise ValueError("component values must be [effective decisions, 3]")
    rewards = component_rewards(lane).detach()
    next_values = torch.zeros_like(values)
    next_values[:-1] = values[1:]
    advantages, targets = [], []
    for column, name in enumerate(COMPONENTS):
        advantage, target = generalized_advantage_estimate(
            rewards[:, column:column+1], values[:, column:column+1],
            next_values[:, column:column+1], lane.done[:, None], lane.truncated[:, None],
            gamma=config.gamma, gae_lambda=getattr(config, name + "_lambda"))
        advantages.append(advantage)
        targets.append(target)
    return torch.cat(advantages, dim=-1), torch.cat(targets, dim=-1)


def resolve_config(checkpoint, *, enabled=False, gamma=.999, lambdas=None):
    lambdas = lambdas or {}
    saved = checkpoint.get(COMPONENT_KEY)
    previous = None if saved is None else ComponentGaeConfig.from_dict(saved["config"])
    if previous is not None:
        if previous.gamma != gamma or any(value is not None and
                value != getattr(previous, name + "_lambda") for name, value in lambdas.items()):
            raise ValueError("component GAE resume configuration mismatch")
        if checkpoint.get("rl_gae_config") != previous.as_dict():
            raise ValueError("component GAE checkpoint metadata mismatch")
        return previous
    if checkpoint.get("rl_gae_config", {}).get("version") == "event-component-gae.v1":
        raise ValueError("missing component critic checkpoint state")
    if not enabled and not any(v is not None for v in lambdas.values()):
        return None
    return ComponentGaeConfig(gamma=gamma, **{name + "_lambda": value
                                            for name, value in lambdas.items() if value is not None})


def load_critic(model, checkpoint, config):
    if config is None:
        return None
    critic = ComponentCritic(model)
    saved = checkpoint.get(COMPONENT_KEY)
    if saved is not None:
        if saved["config"] != config.as_dict():
            raise ValueError("component critic configuration mismatch")
        critic.load_state_dict(saved["critic"], strict=True)
    if any(not bool(torch.isfinite(t).all()) for t in critic.state_dict().values()):
        raise ValueError("nonfinite component critic")
    return critic


def restore_component_optimizer(critic, checkpoint, *, learning_rate=None):
    saved = checkpoint.get(COMPONENT_KEY)
    optimizer = torch.optim.AdamW(critic.parameters(), lr=1e-5 if learning_rate is None else learning_rate)
    if saved is not None:
        if saved.get("parameter_names") != [n for n, _ in critic.named_parameters()]:
            raise ValueError("component optimizer parameter order mismatch")
        for name in ("updates", "optimizer_steps", "decision_rows"):
            if type(saved.get(name)) is not int or saved[name] < 0:
                raise ValueError("invalid component training counters")
        optimizer.load_state_dict(saved["optimizer"])
        for parameter, state in optimizer.state.items():
            for name, tensor in state.items():
                if isinstance(tensor, torch.Tensor):
                    if not bool(torch.isfinite(tensor).all()):
                        raise ValueError("nonfinite component optimizer")
                    if name in {"exp_avg", "exp_avg_sq", "max_exp_avg_sq"} and tensor.shape != parameter.shape:
                        raise ValueError("component optimizer moment shape mismatch")
    if learning_rate is not None:
        for group in optimizer.param_groups:
            group["lr"] = learning_rate
    return optimizer


def component_checkpoint(critic, optimizer, config, parent, stats):
    saved = parent.get(COMPONENT_KEY, {})
    return dict(config=config.as_dict(), critic=critic.state_dict(), optimizer=optimizer.state_dict(),
                parameter_names=[n for n, _ in critic.named_parameters()],
                updates=saved.get("updates", 0)+1,
                optimizer_steps=saved.get("optimizer_steps", 0)+stats.optimizer_steps,
                decision_rows=saved.get("decision_rows", 0)+stats.steps)
