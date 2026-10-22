"""Auditable tower potential, computed on execution-filtered transitions only."""
from __future__ import annotations

import math

TOWER_POTENTIAL = dict(version="visible-tower-hp.v1", coefficient=.1, gamma=.999,
    normalization=5., king_weight=3., princess_weight=1., terminal="zero",
    credit="effective-decision-endpoints.v1")
STRONG_TOWER_POTENTIAL = dict(TOWER_POTENTIAL, coefficient=1.)
MODERATE_TOWER_POTENTIAL = dict(TOWER_POTENTIAL, coefficient=.15)


def validate_tower_state(state):
    if state is None or len(state) != 6:
        raise ValueError("tower evidence requires all six canonical towers")
    expected = {(o * 3 + i, o, kind) for o in (0, 1)
                for i, kind in enumerate(("king", "princess_left", "princess_right"))}
    try:
        identities = {(identity, owner, kind) for identity, owner, kind, hp, initial in state}
        valid = all(type(identity) is int and type(owner) is int
                    and math.isfinite(hp) and math.isfinite(initial)
                    and initial > 0 and 0 <= hp <= initial
                    for identity, owner, kind, hp, initial in state)
    except (TypeError, ValueError):
        raise ValueError("invalid tower evidence fields") from None
    if identities != expected or not valid:
        raise ValueError("invalid canonical tower identity or health evidence")


def tower_state(towers, initial_hp):
    result = tuple(sorted((int(t.entity_id), int(t.owner), t.tower_kind,
                           float(t.hitpoints), float(initial_hp[t.entity_id])) for t in towers))
    validate_tower_state(result)
    return result


def tower_potential(state, owner):
    validate_tower_state(state)
    if owner not in (0, 1):
        raise ValueError("invalid tower reward owner")
    return math.fsum((1. if side != owner else -1.) * (3. if kind == "king" else 1.)
                     * (1. - hp / initial) for _, side, kind, hp, initial in state) / 5.


def tower_config(config):
    value = None if config is None else config.get("tower_potential")
    if value is not None:
        expected = {"elixir-overflow-tower.v3": TOWER_POTENTIAL,
                    "elixir-overflow-tower.v4": STRONG_TOWER_POTENTIAL,
                    "elixir-overflow-tower.v5": STRONG_TOWER_POTENTIAL,
                    "elixir-overflow-tower.v6": MODERATE_TOWER_POTENTIAL}.get(config.get("version"))
        if expected is None or value != expected:
            raise ValueError("unsupported tower potential configuration")
    return value


def validate_reward_gamma(config, gamma):
    shaping = tower_config(config)
    if shaping is not None and gamma != shaping["gamma"]:
        raise ValueError("tower reward gamma differs from learner gamma")


def validate_tower_rows(records, config):
    shaping = tower_config(config)
    initial = None
    for row in records:
        if row.tower_shaping != 0:
            raise ValueError("derived tower reward must not be stored or shaped twice")
        if row.tower_state is None:
            if shaping is not None:
                raise ValueError("tower shaping requires decision-time tower evidence")
            continue
        validate_tower_state(row.tower_state)
        maxima = tuple(sorted((identity, maximum) for identity, _, _, _, maximum in row.tower_state))
        if initial is not None and initial != maxima:
            raise ValueError("initial tower health changed within a lane")
        initial = maxima


def shape_segments(segments, config):
    from dataclasses import replace
    shaping = tower_config(config)
    if shaping is None:
        return segments
    result = []
    for position, (source, row) in enumerate(segments):
        current = tower_potential(row.tower_state, row.owner)
        following = (0. if row.done else
                     tower_potential(segments[position + 1][1].tower_state, row.owner))
        reward = shaping["coefficient"] * (shaping["gamma"] * following - current)
        result.append((source, replace(row, reward=row.reward + reward, tower_shaping=reward)))
    return result


def tower_components(groups, config):
    from .event_rollout import training_segments
    result = dict(tower_shaping=0., tower_change_reward=0., tower_discount_reward=0.,
        tower_terminal_settlement=0., tower_changed_transitions=0, tower_nonzero_transitions=0,
        tower_effective_transitions=0, tower_telescoping_error_max=0.)
    shaping = tower_config(config)
    if shaping is None:
        return result
    extended = config.get("version") in ("elixir-overflow-tower.v4", "elixir-overflow-tower.v5", "elixir-overflow-tower.v6")
    if extended:
        result.update(tower_shaping_abs=0., tower_change_reward_abs=0., tower_discount_reward_abs=0.,
            tower_terminal_settlement_abs=0., tower_terminal_transitions=0,
            tower_discount_only_transitions=0, tower_discount_only_abs=0.)
    for rows in groups:
        segments = training_segments(rows, reward_config=config)
        if not segments:
            continue
        discounted = 0.
        for i, (_, row) in enumerate(segments):
            result["tower_shaping"] += row.tower_shaping
            result["tower_effective_transitions"] += 1
            result["tower_nonzero_transitions"] += int(abs(row.tower_shaping) > 1e-8)
            if extended:
                result["tower_shaping_abs"] += abs(row.tower_shaping)
            discounted += shaping["gamma"] ** i * row.tower_shaping
            if row.done:
                result["tower_terminal_settlement"] += row.tower_shaping
                if extended:
                    result["tower_terminal_settlement_abs"] += abs(row.tower_shaping)
                    result["tower_terminal_transitions"] += 1
            else:
                now = tower_potential(row.tower_state, row.owner)
                nxt = tower_potential(segments[i+1][1].tower_state, row.owner)
                result["tower_change_reward"] += shaping["coefficient"] * (nxt - now)
                result["tower_discount_reward"] += shaping["coefficient"] * (shaping["gamma"] - 1.) * nxt
                result["tower_changed_transitions"] += int(abs(nxt - now) > 1e-8)
                if extended:
                    result["tower_change_reward_abs"] += abs(shaping["coefficient"] * (nxt-now))
                    result["tower_discount_reward_abs"] += abs(shaping["coefficient"] * (shaping["gamma"]-1.) * nxt)
                    if abs(nxt-now) <= 1e-8 and abs(row.tower_shaping) > 1e-8:
                        result["tower_discount_only_transitions"] += 1
                        result["tower_discount_only_abs"] += abs(row.tower_shaping)
        error = abs(discounted + shaping["coefficient"] * tower_potential(segments[0][1].tower_state, rows[0].owner))
        if error > 1e-8:
            raise ValueError("tower potential discounted sum did not telescope")
        result["tower_telescoping_error_max"] = max(result["tower_telescoping_error_max"], error)
    return result
