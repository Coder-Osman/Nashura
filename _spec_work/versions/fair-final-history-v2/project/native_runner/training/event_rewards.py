"""Personal overflow shaping, measured in native integer elixir units.

This is rollout bookkeeping, never an observation feature or a zero-sum reward.
The only supported resource source is the verified natural regeneration schedule.
"""
from __future__ import annotations

from copy import deepcopy
import math

RAW_PER_ELIXIR = 10_000
CAP_RAW = 100_000
OVERFLOW_REWARD_CONFIG = dict(
    version="elixir-overflow.v1", penalty_per_elixir=.02, penalty_cap=.5,
    start="first-policy-decision", scope="whole-match", metering="native-integer.v1",
    terminal_interval="skip-if-end-tick-unknown", credit="previous-effective-decision.v1",
)
UNCAPPED_OVERFLOW_REWARD_CONFIG = dict(
    OVERFLOW_REWARD_CONFIG, version="elixir-overflow.v2", penalty_per_elixir=.01,
    penalty_cap=None,
)
from .event_tower_rewards import TOWER_POTENTIAL, STRONG_TOWER_POTENTIAL, MODERATE_TOWER_POTENTIAL

TOWER_OVERFLOW_REWARD_CONFIG = dict(
    UNCAPPED_OVERFLOW_REWARD_CONFIG, version="elixir-overflow-tower.v3",
    tower_potential=deepcopy(TOWER_POTENTIAL),
)
STRONG_TOWER_OVERFLOW_REWARD_CONFIG = dict(
    UNCAPPED_OVERFLOW_REWARD_CONFIG, version="elixir-overflow-tower.v4",
    tower_potential=deepcopy(STRONG_TOWER_POTENTIAL),
)
LOW_OVERFLOW_TOWER_REWARD_CONFIG = dict(
    deepcopy(STRONG_TOWER_OVERFLOW_REWARD_CONFIG),
    version="elixir-overflow-tower.v5", penalty_per_elixir=.003,
)
MODERATE_TOWER_OVERFLOW_REWARD_CONFIG = dict(
    deepcopy(LOW_OVERFLOW_TOWER_REWARD_CONFIG), version="elixir-overflow-tower.v6",
    tower_potential=deepcopy(MODERATE_TOWER_POTENTIAL),
)


def reward_config(value):
    if value is None:
        return None  # Historical, terminal-only shards remain readable.
    for supported in (OVERFLOW_REWARD_CONFIG, UNCAPPED_OVERFLOW_REWARD_CONFIG,
                      TOWER_OVERFLOW_REWARD_CONFIG, STRONG_TOWER_OVERFLOW_REWARD_CONFIG,
                      LOW_OVERFLOW_TOWER_REWARD_CONFIG, MODERATE_TOWER_OVERFLOW_REWARD_CONFIG):
        if value == supported:
            return deepcopy(supported)
    raise ValueError("unsupported event reward configuration")


def overflow_penalty(waste_raw, config):
    """Total personal cost; None explicitly means no match-wide cap."""
    config = reward_config(config)
    if config is None or type(waste_raw) is not int or waste_raw < 0:
        raise ValueError("overflow penalty needs a config and nonnegative native units")
    penalty = waste_raw / RAW_PER_ELIXIR * config["penalty_per_elixir"]
    cap = config["penalty_cap"]
    return penalty if cap is None else min(cap, penalty)


def raw_elixir(snapshot):
    result = {int(p["owner"]): p["elixirRaw"] for p in snapshot["players"]}
    if set(result) != {0, 1} or any(type(v) is not int or not 0 <= v <= CAP_RAW for v in result.values()):
        raise ValueError("overflow metering requires exact native integer elixir")
    return result


def rate_segments(timeline, start, stop):
    """Integrate the engine's truncated integer rate, not its float estimate."""
    if stop < start:
        raise ValueError("overflow interval runs backwards")
    while start < stop:
        rate = 100_000 * 50 // timeline.elixir_rate(start).full_bar_ms
        if rate not in (178, 357, 537):
            raise ValueError("unverified native elixir rate")
        end = min(stop, timeline.next_elixir_boundary(start) or stop)
        if end <= start:
            raise ValueError("invalid native elixir boundary")
        yield start, end, rate, {178: "1", 357: "2", 537: "3"}[rate]
        start = end


class ElixirOverflowMeter:
    def __init__(self, timeline, config):
        self.config = reward_config(config)
        if self.config is None:
            raise ValueError("overflow meter needs an explicit reward configuration")
        self.timeline = timeline
        self.started = {0: None, 1: None}
        self.receipts = {}
        self.owners = {o: dict(waste_raw=0, penalty=0., measured_ticks=0, skipped_terminal_ticks=0,
            first_decision_tick=None, first_play_tick=None, single_elixir_plays=0,
            by_multiplier={str(m): dict(waste_raw=0, penalty=0., ticks=0, plays=0) for m in (1, 2, 3)})
            for o in (0, 1)}

    def start(self, owner, tick):
        if self.started[owner] is None:
            self.started[owner] = tick
            self.owners[owner]["first_decision_tick"] = tick

    def advance(self, *, start, stop, before, after, outcomes=(), gameplay_before=True, gameplay_after=True):
        if stop < start:
            raise ValueError("overflow interval runs backwards")
        spends = {0: [], 1: []}
        for event in outcomes:
            if event.event_type != "action_executed":
                continue
            identity = event.data.get("action_id")
            cost = event.data.get("effective_cost")
            if not identity or cost is None or not math.isfinite(cost) or cost < 0 or cost * RAW_PER_ELIXIR != round(cost * RAW_PER_ELIXIR):
                raise ValueError("overflow requires an identified, confirmed native card cost")
            value = (event.owner, int(event.tick), round(cost * RAW_PER_ELIXIR))
            if identity in self.receipts:
                if self.receipts[identity] != value:
                    raise ValueError("conflicting duplicate elixir receipt")
                continue
            if value[0] not in (0, 1) or not start < value[1] <= stop:
                raise ValueError("elixir receipt outside the measured interval")
            self.receipts[identity] = value
            spends[event.owner].append(value)
            data = self.owners[event.owner]
            if data["first_play_tick"] is None:
                data["first_play_tick"] = int(event.tick)
            # Receipt tick is the resulting snapshot: the native engine spends
            # first, then regenerates during [receipt_tick-1, receipt_tick).
            multiplier = next(rate_segments(self.timeline, event.tick-1, event.tick))[3]
            data["by_multiplier"][multiplier]["plays"] += 1
            data["single_elixir_plays"] += int(multiplier == "1")
        penalties = {0: 0., 1: 0.}
        for owner in (0, 1):
            if self.started[owner] is None or not gameplay_before:
                continue
            if self.started[owner] > start:
                raise ValueError("meter must start at an observed policy decision boundary")
            data = self.owners[owner]
            if not gameplay_after:
                # The later finalized snapshot need not expose the exact death tick.
                data["skipped_terminal_ticks"] += stop - start
                continue
            segments = list(rate_segments(self.timeline, start, stop))
            generated = sum((b-a)*rate for a, b, rate, _ in segments)
            spent = sum(item[2] for item in spends[owner])
            residual = before[owner] + generated - spent - after[owner]
            if residual < 0 or residual > max(0, before[owner]+generated-CAP_RAW):
                raise ValueError(f"native elixir conservation failed: owner={owner}, ticks={start}:{stop}, "
                                 f"before={before[owner]}, generated={generated}, spent={spent}, after={after[owner]}")
            # Driver stops at rate boundaries. Cross-boundary audit callers may
            # also use this meter; replay confirmed costs to attribute each phase.
            if len(segments) <= 1:
                allocations = [(segments[0][3], residual, stop-start)] if segments else []
            else:
                bar, cursor, allocations = before[owner], start, []
                events = sorted(spends[owner], key=lambda item: item[1])
                for a, b, rate, multiplier in segments:
                    waste = 0
                    while events and events[0][1]-1 < b:
                        _, receipt_tick, cost = events.pop(0)
                        when = receipt_tick-1
                        amount = max(0, when-cursor)*rate
                        waste += max(0, bar+amount-CAP_RAW)
                        bar = min(CAP_RAW, bar+amount)-cost
                        cursor = when
                    amount = (b-cursor)*rate
                    waste += max(0, bar+amount-CAP_RAW)
                    bar = min(CAP_RAW, bar+amount)
                    cursor = b
                    allocations.append((multiplier, waste, b-a))
                if sum(w for _, w, _ in allocations) != residual:
                    raise ValueError("cross-rate native elixir allocation disagrees with conservation")
            for multiplier, waste, ticks in allocations:
                old_penalty = overflow_penalty(data["waste_raw"], self.config)
                data["waste_raw"] += waste
                new_penalty = overflow_penalty(data["waste_raw"], self.config)
                penalty = new_penalty-old_penalty
                data["penalty"] = new_penalty
                data["measured_ticks"] += ticks
                phase = data["by_multiplier"][multiplier]
                phase["waste_raw"] += waste
                phase["penalty"] += penalty
                phase["ticks"] += ticks
                penalties[owner] += penalty
        return penalties

    def report(self):
        return dict(config=self.config, owners={str(o): dict(deepcopy(value),
            waste_elixir=value["waste_raw"]/RAW_PER_ELIXIR,
            cap_reached=(self.config["penalty_cap"] is not None
                         and value["penalty"] >= self.config["penalty_cap"]),
            first_play_seconds=None if value["first_play_tick"] is None else value["first_play_tick"]*.05,
            single_elixir_zero_plays=value["single_elixir_plays"] == 0)
            for o, value in self.owners.items()}, receipt_count=len(self.receipts))


def reward_components(records, config=None):
    """Audit raw, learnable and excluded-prefix rewards without future credit."""
    groups = {}
    for row in records:
        groups.setdefault((row.match_id, row.owner), []).append(row)
    result = dict(base_reward=0., overflow_penalty=0., training_reward=0.,
                  effective_overflow_penalty=0., prefix_overflow_penalty=0.,
                  effective_training_reward=0., prefix_training_reward=0.)
    for rows in groups.values():
        first = next((i for i, row in enumerate(rows) if row.trainable), len(rows))
        for i, row in enumerate(rows):
            result["base_reward"] += row.reward if row.base_reward is None else row.base_reward
            result["overflow_penalty"] += row.overflow_penalty
            result["training_reward"] += row.reward
            prefix = "prefix" if i < first else "effective"
            result[prefix+"_overflow_penalty"] += row.overflow_penalty
            result[prefix+"_training_reward"] += row.reward
    if config is not None and "tower_potential" in config:
        from .event_tower_rewards import tower_components
        components = tower_components(groups.values(), config)
        result.update(components)
        result["training_reward"] += components["tower_shaping"]
        result["effective_training_reward"] += components["tower_shaping"]
    return result
