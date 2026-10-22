"""Fail-closed checks of real rollout data before any event-policy update."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict, field
import math
from pathlib import Path
from typing import Sequence

import torch

from .event_cache import load_event_rl_shard
from .event_config import EVENT_DELAY_OFFSETS_MS
from .event_model import EventCardPolicyV1
from .event_ppo import EventLaneRolloutV1, _lanes_on
from .event_batches import PreparedEventRollout, row_batches
from .event_rollout import lanes_from_shard, EXECUTION_CREDIT_CONTRACT
from .event_rewards import reward_config, reward_components


class EventPreflightError(ValueError):
    """A rejected update, with machine-readable diagnostics."""

    def __init__(self, message: str, metrics: dict[str, object]):
        super().__init__(message)
        self.metrics = metrics


def load_complete_rollout(
    summaries: Sequence[Path],
    *,
    expected_checkpoint_sha256: str | None = None,
    expected_model_contract_hash: str | None = None,
    row_metadata: list[list[dict[str, object]]] | None = None,
    match_plan: dict | None = None,
    expected_minimum_delay_ms: int | None = None,
    shard_cache=None,
) -> tuple[list[EventLaneRolloutV1], dict[str, object]]:
    """Load every selected shard and reject the entire update on partial lanes.

    A lane is one (shard, match, owner), as in ``lanes_from_shard``. Shards
    must contain complete episodes; we do not stitch fragments or treat a tick
    cap as a terminal outcome. Inspect metadata before assembly, whose validator
    also requires complete lanes. Validate row temperatures before assembly can
    collapse them to the first row's tuple. If supplied, ``row_metadata`` is
    replaced on successful loading with diagnostic rows aligned to returned
    lanes. Pass it to ``preflight_event_rollout`` to retain source IDs and heads.
    """
    shards = []
    expected_matches = {}
    if match_plan is not None:
        from .event_opponents import validate_plan, match_metadata, validate_shard_ownership
        validate_plan(match_plan)
        if match_plan["current_sha256"] != expected_checkpoint_sha256:
            raise EventPreflightError("mixed plan learner checkpoint mismatch", {})
        expected_matches = {(m["host"], m["port"]): match_metadata(match_plan, m) for m in match_plan["matches"]}
    seen_matches = set()
    counts = dict(shards=0, rows=0, lanes=0, complete_lanes=0,
                  incomplete_lanes=0, truncated_lanes=0, rejected_lanes=0,
                  rejected_rows=0, inconsistent_lanes=0, trainable_rows=0,
                  excluded_rows=0, empty_training_lanes=0, execution_status_counts={},
                  execution_credit_contract=EXECUTION_CREDIT_CONTRACT)
    for summary in summaries:
        shard = (load_event_rl_shard if shard_cache is None else shard_cache.load)(summary, verify=True)
        shards.append(shard)
        counts["shards"] += 1
        counts["rows"] += len(shard.records)
        for row in shard.records:
            status = row.execution_status
            counts["execution_status_counts"][status] = counts["execution_status_counts"].get(status, 0) + 1
            counts["trainable_rows"] += int(row.trainable)
            counts["excluded_rows"] += int(not row.trainable)
        if any(row.execution_status == "legacy_unknown" for row in shard.records):
            counts["rejected_rows"] += len(shard.records)
            raise EventPreflightError(
                "rollout lacks execution evidence; recollect before training; no update performed", counts)
        # Check both levels before lane assembly or any model scoring. Human
        # policy labels and matching probabilities are not checkpoint identity.
        contract = shard.manifest.get("contract", {})
        configured_rewards = reward_config(contract.get("reward_config"))
        if len(shards) == 1:
            counts["reward_config"] = configured_rewards
            counts["reward_components"] = {key: 0. for key in reward_components([], configured_rewards)}
        elif counts["reward_config"] != configured_rewards:
            raise EventPreflightError("rollout reward configuration mismatch; no update performed", counts)
        if any((r.base_reward is None) != (configured_rewards is None) for r in shard.records):
            raise EventPreflightError("rollout reward components disagree with configuration", counts)
        components = reward_components(shard.records, configured_rewards)
        for key, value in components.items():
            if key == "tower_telescoping_error_max":
                counts["reward_components"][key] = max(counts["reward_components"][key], value)
            else:
                counts["reward_components"][key] += value
        if match_plan is not None:
            key = (summary.parent.parent.name.removeprefix("host-"),
                   int(summary.parent.name.removeprefix("port-")))
            if key not in expected_matches or key in seen_matches:
                raise EventPreflightError("unexpected or duplicate mixed match shard", counts)
            validate_shard_ownership(shard, expected_matches[key])
            seen_matches.add(key)
        elif contract.get("mixed_opponents") is not None:
            raise EventPreflightError("mixed opponent shard requires its frozen round plan", counts)
        # Legacy shards have no policy constraints. Never mix them with a
        # constrained rollout, or combine different margins in one update.
        constraints = contract.get("action_constraints")
        if expected_minimum_delay_ms is not None:
            required = {"minimum_ms": expected_minimum_delay_ms}
            if not isinstance(constraints, dict) or constraints.get("delay") != required:
                counts["rejected_rows"] += len(shard.records)
                raise EventPreflightError(
                    f"rollout delay constraint missing or mismatched in {summary}; recollect before training", counts)
            forbidden = torch.tensor([value < expected_minimum_delay_ms for value in EVENT_DELAY_OFFSETS_MS])
            masks = shard.delay_masks
            if (masks is None or masks.shape[-1] != len(EVENT_DELAY_OFFSETS_MS)
                    or bool(masks[..., forbidden].any())
                    or any(row.act and row.ability_candidate < 0 and (not 0 <= row.delay_bin < len(EVENT_DELAY_OFFSETS_MS)
                                        or EVENT_DELAY_OFFSETS_MS[row.delay_bin] < expected_minimum_delay_ms)
                           for row in shard.records)):
                counts["rejected_rows"] += len(shard.records)
                raise EventPreflightError(
                    f"rollout contains a forbidden delay or delay mask in {summary}; no update performed", counts)
        if len(shards) == 1:
            counts["action_constraints"] = constraints
        elif constraints != counts["action_constraints"]:
            counts["rejected_rows"] += len(shard.records)
            raise EventPreflightError(
                f"rollout action_constraints mismatch in {summary}; no update performed", counts,
            )
        for field, expected in (
            ("behavior_checkpoint_sha256", expected_checkpoint_sha256),
            ("behavior_model_contract_hash", expected_model_contract_hash),
        ):
            if expected is None:
                continue
            if not expected or contract.get(field) != expected or any(
                getattr(row, field) != expected for row in shard.records
            ):
                counts["rejected_rows"] += len(shard.records)
                raise EventPreflightError(
                    f"rollout behavior identity mismatch or missing {field} in {summary}; "
                    "no update performed", counts,
                )
        groups = defaultdict(list)
        for row in shard.records:
            groups[(row.match_id, row.owner)].append(row)
        for rows in groups.values():
            cap = None if configured_rewards is None else configured_rewards["penalty_cap"]
            if cap is not None and sum(r.overflow_penalty for r in rows) > cap + 1e-9:
                raise EventPreflightError("per-owner overflow penalty exceeds match cap", counts)
            counts["lanes"] += 1
            counts["empty_training_lanes"] += int(not any(row.trainable for row in rows))
            truncated = any(row.truncated for row in rows)
            incomplete = not rows[-1].done or any(row.done for row in rows[:-1])
            inconsistent = (
                len({tuple(row.temperatures) for row in rows}) != 1
                or len({row.policy_version for row in rows}) != 1
                or any(b.observation_tick <= a.observation_tick for a, b in zip(rows, rows[1:]))
            )
            counts["truncated_lanes"] += int(truncated)
            counts["incomplete_lanes"] += int(incomplete)
            counts["inconsistent_lanes"] += int(inconsistent)
            if truncated or incomplete or inconsistent:
                counts["rejected_lanes"] += 1
                counts["rejected_rows"] += len(rows)
            else:
                counts["complete_lanes"] += 1
    if match_plan is not None:
        if seen_matches != set(expected_matches) or counts["lanes"] != match_plan["expected_player_lanes"]:
            raise EventPreflightError("mixed round missing matches or learner lanes", counts)
        counts["mixed_opponents"] = dict(version=match_plan["version"], plan_sha256=match_plan["sha256"],
            iteration=match_plan["iteration"], pool=match_plan["pool"], games=len(seen_matches),
            expected_player_lanes=match_plan["expected_player_lanes"])
    if not counts["rows"] or counts["rejected_lanes"]:
        raise EventPreflightError(
            "rollout requires complete, untruncated, ordered lanes with constant "
            "temperatures and policy version; no update performed", counts,
        )
    lanes = [lane for shard in shards for lane in lanes_from_shard(shard)]
    if sum(lane.time_steps for lane in lanes) != counts["trainable_rows"]:
        raise EventPreflightError("lane assembly did not preserve every trainable row", counts)
    if not lanes:
        raise EventPreflightError("rollout has no trainable decisions; no update performed", counts)
    counts["training_lanes"] = len(lanes)
    if match_plan is not None and match_plan["version"] == "mixed-deck.v1":
        from .mixed_decks import row_counts
        counts["deck_rows"] = row_counts(lanes, match_plan["learning_weights"])
        counts["deck_lanes"] = {d: sum(l.deck_id == d for l in lanes) for d in counts["deck_rows"]}
        counts["mixed_opponents"].update({k: match_plan[k] for k in (
            "mixed_round", "phase", "learning_weights", "rules", "recent_fallback")})
        if "deck_policy" in match_plan:
            counts["mixed_opponents"]["deck_policy"] = match_plan["deck_policy"]
        if "allocation_policy" in match_plan:
            counts["mixed_opponents"]["allocation_policy"] = match_plan["allocation_policy"]
        counts["mixed_opponents"].update(deck_rows=counts["deck_rows"], deck_lanes=counts["deck_lanes"],
            actual_row_proportions={d: n / counts["trainable_rows"] for d, n in counts["deck_rows"].items()})
    if row_metadata is not None:
        metadata = []
        for summary, shard in zip(summaries, shards):
            # Match lanes_from_shard's owner-major, first-seen match ordering.
            for owner in (0, 1):
                groups = defaultdict(list)
                for index, row in enumerate(shard.records):
                    if row.owner != owner or not row.trainable:
                        continue
                    groups[row.match_id].append(dict(
                        summary_path=str(summary), shard_id=shard.manifest.get("shard_id"),
                        shard_row_index=index, match_id=row.match_id, owner=row.owner,
                        observation_tick=row.observation_tick, policy_version=row.policy_version,
                        behavior_checkpoint_sha256=getattr(row, "behavior_checkpoint_sha256", None),
                        behavior_model_contract_hash=getattr(row, "behavior_model_contract_hash", None),
                        old_head_log_probs={head: getattr(row, f"{head}_log_prob", None)
                                            for head in _HEADS},
                    ))
                metadata.extend(groups.values())
        row_metadata[:] = metadata
    return lanes, counts


_HEADS = ("gate", "card", "cell", "delay", "ability")


def _finite_float(value: object) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _difference_summary(values: list[float], max_threshold: float,
                        mean_threshold: float) -> dict[str, object]:
    """Finite absolute differences; mean-threshold counts are descriptive only."""
    data = torch.tensor(values, dtype=torch.float64)
    return dict(
        finite_rows=len(values),
        max_abs_difference=max(values) if values else None,
        mean_abs_difference=float(data.mean()) if values else None,
        abs_difference_quantiles={name: float(torch.quantile(data, q)) if values else None
                                 for name, q in (("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99))},
        max_threshold=max_threshold, mean_threshold=mean_threshold,
        rows_above_max_threshold=sum(value > max_threshold for value in values),
        rows_above_mean_threshold=sum(value > mean_threshold for value in values),
    )


@dataclass(frozen=True)
class EventPreflightStats:
    lanes: int
    rows: int
    finite_rows: int
    nonfinite_rows: int
    max_abs_log_ratio: float | None
    mean_abs_log_ratio: float | None
    ratio_mean: float | None
    ratio_min: float | None
    ratio_max: float | None
    clip_fraction: float | None
    max_abs_log_ratio_tolerance: float
    mean_abs_log_ratio_tolerance: float
    clip_ratio: float
    passed: bool
    diagnostics: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@torch.no_grad()
def preflight_event_rollout(
    model: EventCardPolicyV1,
    lanes: Sequence[EventLaneRolloutV1],
    *,
    chunk_steps: int = 32,
    clip_ratio: float = 0.2,
    max_abs_log_ratio: float = 0.01,
    mean_abs_log_ratio: float = 0.001,
    expected_temperatures: tuple[float, float, float, float] | None = None,
    row_metadata: Sequence[Sequence[dict[str, object]]] | None = None,
    diagnostic_top_rows: int = 10,
    prepared: PreparedEventRollout | None = None,
    component_critic=None,
) -> EventPreflightStats:
    """Rescore ALL loaded decisions under the unchanged checkpoint in eval mode.

    FP16 observation serialization perturbs inputs slightly. The explicit max
    and row-weighted mean tolerances allow that round trip, never an automatic
    retry or tolerance increase. Nonfinite probabilities/differences/ratios or
    any PPO clipping always reject. Diagnostics on nonfinite input summarize
    finite rows only, with separate counts and ``passed=False``.
    """
    if prepared is not None:
        prepared.clear_values()
        prepared.validate(model, lanes)
    for name, value in (("max_abs_log_ratio", max_abs_log_ratio),
                        ("mean_abs_log_ratio", mean_abs_log_ratio), ("clip_ratio", clip_ratio)):
        if not math.isfinite(value) or value < 0 or (name == "clip_ratio" and value == 0):
            raise ValueError(f"{name} must be finite and {'positive' if name == 'clip_ratio' else 'nonnegative'}")
    if chunk_steps < 1 or not lanes:
        raise ValueError("preflight needs lanes and a positive chunk size")
    # Validate all lanes before encoding even the first one.
    for lane in lanes:
        lane.validate()
        if not bool(lane.done[-1]) or bool(lane.truncated.any()):
            raise ValueError("preflight requires complete, untruncated lanes")
        if len(lane.temperatures) != 4 or any(not math.isfinite(t) or t <= 0 for t in lane.temperatures):
            raise ValueError("preflight temperatures must be four finite positive values")
        if expected_temperatures is not None and tuple(lane.temperatures) != tuple(expected_temperatures):
            raise ValueError(f"rollout temperatures {lane.temperatures} do not match expected {expected_temperatures}")

    if diagnostic_top_rows < 1:
        raise ValueError("diagnostic_top_rows must be positive")
    if row_metadata is not None and (len(row_metadata) != len(lanes) or any(
        len(metadata) != lane.time_steps for metadata, lane in zip(row_metadata, lanes)
    )):
        raise ValueError("preflight row metadata must align with every lane and row")
    joint_values: list[float] = []
    heads = _HEADS if model.config.ability_actions else _HEADS[:-1]
    head_values: dict[str, list[float]] = {head: [] for head in heads}
    head_available = dict.fromkeys(heads, 0)
    top_rows: list[dict[str, object]] = []
    top_heads: dict[str, list[dict[str, object]]] = {head: [] for head in heads}

    def retain(target, row, key):
        target.append(row)
        # Nonfinite rows come first, followed by the largest finite differences.
        target.sort(key=lambda item: (item[key] is None, item[key] or 0), reverse=True)
        del target[diagnostic_top_rows:]

    rows = finite_rows = clipped = 0
    abs_sum = ratio_sum = maximum = 0.0
    ratio_minimum, ratio_maximum = math.inf, -math.inf
    was_training = model.training
    model.eval()
    try:
        device = next(model.parameters()).device
        batches = prepared.batches if prepared is not None else row_batches(
            _lanes_on(lanes, device), chunk_steps=chunk_steps)
        cached_values = []
        for batch in batches:
            context = batch.encode(model)
            evaluated = batch.evaluate(model, context)
            if prepared is not None:
                from .event_component_gae import critic_values
                cached_values.append(critic_values(context, component_critic).detach())
            scored = evaluated.log_prob
            old = batch.old_log_prob
            if scored.shape != old.shape:
                raise ValueError("preflight joint log probabilities do not align with rollout rows")
            difference = scored.double() - old.double()
            ratio = difference.exp()
            finite = torch.isfinite(scored) & torch.isfinite(old) & torch.isfinite(difference) & torch.isfinite(ratio)
            rows += batch.stop - batch.start
            finite_rows += int(finite.sum())
            joint_values.extend(difference[finite].abs().cpu().tolist())
            current_heads = {head: getattr(evaluated, f"{head}_log_prob", None) for head in heads}
            current_heads = {head: value.detach().cpu().tolist() if value is not None else None
                             for head, value in current_heads.items()}
            for offset, (old_value, new_value, delta, row_ratio, is_finite) in enumerate(zip(
                old.cpu().tolist(), scored.cpu().tolist(), difference.cpu().tolist(),
                ratio.cpu().tolist(), finite.cpu().tolist(),
            )):
                lane_index, row_index = batch.row_ids[offset]
                metadata = row_metadata[lane_index][row_index] if row_metadata is not None else {}
                identity = dict(lane_index=lane_index, row_index=row_index)
                for key in ("summary_path", "shard_id", "shard_row_index", "match_id", "owner",
                            "observation_tick", "policy_version", "behavior_checkpoint_sha256",
                            "behavior_model_contract_hash"):
                    identity[key] = metadata.get(key)
                head_differences = {}
                old_heads = metadata.get("old_head_log_probs") or {}
                for head in heads:
                    previous = old_heads.get(head)
                    current = current_heads[head]
                    if previous is None or current is None:
                        continue
                    head_available[head] += 1
                    head_delta = _finite_float(float(current[offset]) - float(previous))
                    magnitude = abs(head_delta) if head_delta is not None else None
                    head_differences[head] = dict(
                        old_log_prob=_finite_float(previous), new_log_prob=_finite_float(current[offset]),
                        difference=head_delta, abs_difference=magnitude,
                    )
                    if magnitude is not None:
                        head_values[head].append(magnitude)
                    retain(top_heads[head], dict(identity, **head_differences[head]), "abs_difference")
                retain(top_rows, dict(
                    identity, old_log_prob=_finite_float(old_value), new_log_prob=_finite_float(new_value),
                    log_ratio=_finite_float(delta), abs_log_ratio=abs(delta) if is_finite else None,
                    ratio=_finite_float(row_ratio), finite=is_finite,
                    exceeds_max_tolerance=abs(delta) > max_abs_log_ratio if is_finite else None,
                    exceeds_mean_tolerance=abs(delta) > mean_abs_log_ratio if is_finite else None,
                    clipped=abs(row_ratio - 1) > clip_ratio if is_finite else None,
                    heads=head_differences,
                ), "abs_log_ratio")
            if bool(finite.any()):
                absolute = difference[finite].abs()
                ratios = ratio[finite]
                abs_sum += float(absolute.sum())
                maximum = max(maximum, float(absolute.max()))
                ratio_sum += float(ratios.sum())
                ratio_minimum = min(ratio_minimum, float(ratios.min()))
                ratio_maximum = max(ratio_maximum, float(ratios.max()))
                clipped += int(((ratios - 1.0).abs() > clip_ratio).sum())
    finally:
        model.train(was_training)
    mean = abs_sum / finite_rows if finite_rows else None
    passed = (finite_rows == rows and finite_rows > 0 and maximum <= max_abs_log_ratio
              and mean <= mean_abs_log_ratio and clipped == 0)
    stats = EventPreflightStats(
        lanes=len(lanes), rows=rows, finite_rows=finite_rows, nonfinite_rows=rows - finite_rows,
        max_abs_log_ratio=maximum if finite_rows else None, mean_abs_log_ratio=mean,
        ratio_mean=ratio_sum / finite_rows if finite_rows else None,
        ratio_min=ratio_minimum if finite_rows else None, ratio_max=ratio_maximum if finite_rows else None,
        clip_fraction=clipped / finite_rows if finite_rows else None,
        max_abs_log_ratio_tolerance=max_abs_log_ratio,
        mean_abs_log_ratio_tolerance=mean_abs_log_ratio, clip_ratio=clip_ratio, passed=passed,
        diagnostics=dict(
            joint=dict(_difference_summary(joint_values, max_abs_log_ratio, mean_abs_log_ratio),
                       nonfinite_rows=rows - finite_rows, clipped_rows=clipped,
                       top_rows=top_rows),
            heads={head: dict(
                _difference_summary(head_values[head], max_abs_log_ratio, mean_abs_log_ratio),
                available_rows=head_available[head], missing_rows=rows - head_available[head],
                nonfinite_rows=head_available[head] - len(head_values[head]),
                top_rows=top_heads[head],
            ) for head in heads},
            head_thresholds_diagnostic_only=True,
            mean_threshold_row_counts_diagnostic_only=True,
        ),
    )
    if not passed:
        raise EventPreflightError(
            "checkpoint/rollout preflight mismatch: nonfinite scores, log-ratio tolerance exceeded, "
            "or nonzero clipping; no update performed", stats.as_dict(),
        )
    if prepared is not None:
        prepared.retain_values(model, cached_values, component_critic=component_critic)
    return stats
