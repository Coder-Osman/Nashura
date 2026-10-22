"""Self-play rollout driver for event-model RL.

Drives matches with the *sampling* path (`EventCardPolicyV1.select_action`) and
writes one RL shard per match: per-decision behaviour actions, their joint and
per-head log-probs, the four sampling temperatures, and the semi-Markov reward
that the terminal result lands on.

Three things differ from the renderer (``demo_event_selfplay.py``), which this
loop is otherwise modelled on because that loop's plan/cancel handling against
the resident engines is the proven one:

* actions are **sampled**, not argmaxed, so the stored log-probs belong to a
  stochastic behaviour policy — an on-policy requirement for PPO;
* legality is resolved through ``event_action_space`` and fed to the sampler as
  masks, so a sampled action is something the engine can accept;
* every decision becomes a row in an ``EventLaneRecorderV1``, which owns the
  credit assignment (a step's transition runs to the actor's next decision) and
  the shard write.

Every match runs to its natural end. A truncated episode reports no result, so
the win/loss reward is identically zero and the episode teaches nothing.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

# One rollout process per engine slot: letting torch fan out across every core
# makes a fleet of them fight each other, which is what turned a 64-core box into
# load 270 with 96% idle CPU.
torch.set_num_threads(1)

from native_runner.battle_env import BattleEnvV1  # noqa: E402
from native_runner.contracts import ActionV1  # noqa: E402
from native_runner.match_factory import (  # noqa: E402
    NATIVE_FINALIZATION_DEADLINE_TICK,
    MatchConfig,
    tournament_level_kwargs,
)
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog  # noqa: E402
from native_runner.training.event_action_space import (  # noqa: E402
    event_cell_to_native,
    legal_event_actions,
    mask_decision_ready,
    native_hand_slots,
)
from native_runner.training.event_cache import canonicalize_event_observation, default_event_contract  # noqa: E402
from native_runner.training.event_config import EventPolicyConfigV1  # noqa: E402
from native_runner.training.event_spell_constraints import (  # noqa: E402
    LOG_CARD_ID,
    LightningTargetConstraintV1,
    LogTargetConstraintV1,
    action_constraints_contract,
    add_lightning_target_arguments,
    add_log_target_arguments,
    lightning_constraint_from_args,
    log_constraint_from_args,
    spell_target_allowed,
)
from native_runner.training.event_checkpoint import load_event_checkpoint  # noqa: E402
from native_runner.training.event_abilities import (  # noqa: E402
    ability_action_from_plan, ability_plan, candidate_ability_mask,
)
from native_runner.training.event_rollout import EventLaneRecorderV1  # noqa: E402
from native_runner.training.event_scheduling import (  # noqa: E402
    EVENT_SCHEDULER_VERSION, EventExecutionLedger, complete_event_commands,
    gameplay_end_tick, policy_actions_open,
)
from native_runner.training.v4.expert import POLICY_DECISION_TICKS  # noqa: E402
from native_runner.training.v4.factory import (  # noqa: E402
    build_episode_tensorizer_v4,
    build_resident_training_proxies_v4,
    production_semantic_bundle,
)

RESET_OPTIONS = {
    "render_mode": "headless",
    "event_driven_decisions": False,
    "allow_initial_remaining_runtime_rejections": True,
}
TICK_MS = 50


def wait_span(tick: int, plans, event_ticks, last_decision_tick: int, max_advance: int, max_wait: int) -> int:
    """Ticks for one native round-trip, bounded by the next moment we must see.

    Copied from the renderer, where it is verified against a per-tick loop:
    submitting a queued plan at its scheduled tick, seeing the public event it
    produces, and the safety wake that keeps an idle match moving are the three
    things that can come due.
    """

    bounds = [int(max_advance), max(1, int(max_wait) - (tick - int(last_decision_tick)))]
    bounds.extend(max(1, int(plan["scheduled_tick"]) - tick) for plan in plans if plan is not None)
    bounds.extend(max(1, int(when) - tick) for when in event_ticks if int(when) > tick)
    return max(1, min(bounds))


def _deck_registry() -> dict[str, tuple[int, ...]]:
    import importlib.util

    path = Path(__file__).resolve().parent / "league_decks.py"
    spec = importlib.util.spec_from_file_location("_league_decks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {**module.LEAGUE_DECKS, **getattr(module, "RL_LEAGUE_DECKS", {})}


def play_match(
    *,
    environment,
    coordinator,
    model,
    opponent_model=None,
    learner_owner: int = 0,
    event_config: EventPolicyConfigV1,
    catalog,
    device: torch.device,
    decks: Mapping[int, tuple[int, ...]],
    match_id: str,
    policy_version: str,
    seed: int,
    env_id: int,
    temperatures: tuple[float, float, float, float],
    max_ticks: int,
    max_seconds: float,
    max_advance_ticks: int,
    max_wait_ticks: int,
    ignore_window: bool = False,
    first_match: bool = True,
    behavior_checkpoint_sha256: str | None = None,
    behavior_model_contract_hash: str | None = None,
    lightning_constraint: LightningTargetConstraintV1 | None = None,
    log_constraint: LogTargetConstraintV1 | None = None,
    inference_client=None,
    opponent_inference_client=None,
    sampling_generator=None,
    defer_observations: bool = False,
    reward_config=None,
    decision_observer=None,
) -> tuple[EventLaneRecorderV1, dict[str, object]]:
    """Play one match to its natural end and return its recorded rows."""

    if event_config.delay_offset_ms[-1] == 3000:
        max_wait_ticks = event_config.max_wait_ticks
    if learner_owner not in (0, 1):
        raise ValueError("learner_owner must be 0 or 1")
    history = opponent_model is not None
    if opponent_inference_client is not None and (not history or inference_client is None):
        raise ValueError("historical inference routing requires both models and clients")
    recorded_owners = (learner_owner,) if history else (0, 1)
    from native_runner.training.event_rewards import ElixirOverflowMeter, raw_elixir, reward_config as checked_rewards
    reward_config = checked_rewards(reward_config)
    overflow = None
    if reward_config is not None:
        from native_runner.training.mixed_decks import DECKS
        if any(tuple(deck) not in DECKS.values() for deck in decks.values()):
            raise ValueError("overflow metering requires a verified deck without special elixir sources")

    width = event_config.board_width
    height = event_config.board_height
    recorder = EventLaneRecorderV1(
        match_id=match_id,
        policy_version=policy_version,
        temperatures=temperatures,
        decks=decks,
        behavior_checkpoint_sha256=behavior_checkpoint_sha256,
        behavior_model_contract_hash=behavior_model_contract_hash,
        action_constraints=action_constraints_contract(lightning_constraint, log_constraint),
        reward_config=reward_config,
    )
    match_config = MatchConfig(
        deck0=list(decks[0]),
        deck1=list(decks[1]),
        seed=int(seed),
        # Full length: a truncated episode has no winner, so the reward would be
        # zero everywhere and the episode would teach nothing.
        end_tick=NATIVE_FINALIZATION_DEADLINE_TICK,
        **tournament_level_kwargs(),
    )
    observations, _info = environment.reset(
        match_config=match_config, options={**RESET_OPTIONS, "decision_ticks": POLICY_DECISION_TICKS}
    )
    from native_runner.training.event_tower_rewards import tower_state
    initial_tower_hp = ({t.entity_id: t.max_hitpoints for t in observations[0].towers}
                        if reward_config is not None else {})
    if reward_config is not None:
        overflow = ElixirOverflowMeter(environment.mode_timeline.timeline, reward_config)
    initial_elixir = {
        owner: next(p.elixir_exact for p in observations[owner].players if p.owner == owner) for owner in (0, 1)
    }
    tensorizers = {}
    for owner in (0, 1):
        tensorizer = build_episode_tensorizer_v4(environment.episode_config, actor_owner=owner)
        tensorizer.start_episode(observations[owner], initial_elixir=initial_elixir)
        tensorizers[owner] = tensorizer
    coordinator.start_session()
    if not first_match:
        # The resident's rich telemetry only becomes readable again after one
        # step following a rebind, so a second match in the same process must
        # prime it - otherwise every match after the first dies on
        # "requires at least one step after this env is rebound".
        coordinator.begin_round((env_id,))
        try:
            observations, _rewards, _terms, _truncs, _infos = environment.step(
                {owner: (ActionV1.wait(owner, ticks=1),) for owner in (0, 1)},
                record_trace=False,
                policy_features_only=True,
            )
        except BaseException:
            coordinator.abort_round(RuntimeError("priming step failed"))
            raise
        else:
            coordinator.finish_round()

    plans: dict[int, dict | None] = {0: None, 1: None}
    ledger = EventExecutionLedger()
    recovery_owners: set[int] = set()
    event_ticks: list[int] = []
    seen_events: set[str] = set()
    counters = {
        "scheduler_version": EVENT_SCHEDULER_VERSION,
        "events": 0, "wake": 0, "executed": 0, "cancelled": 0, "steps": 0, "decisions": 0,
        "not_ready": 0, "cancelled_window": 0, "cancelled_cell": 0, "rows_without_execution": 0,
        "cancelled_hand_slot": 0, "cancelled_play_kind": 0,
        "cancelled_lightning_target": 0,
        "cancelled_log_target": 0,
        "recovery": 0, "cancelled_gameplay_end": 0,
        "decisions_after_gameplay": 0, "submissions_after_gameplay": 0,
        "deferred_for_elixir": 0, "act_impossible_states": 0, "act_violation": 0,
        "ability_events": 0, "ability_decisions": 0, "cancelled_ability": 0,
        "opponent": "history" if history else "self",
    }
    actors = {owner: model if owner in recorded_owners else opponent_model for owner in (0, 1)}
    # Split the ACT rate by what triggered the decision and by whether a play was
    # even legal: a low headline ACT share can be a population effect, not a
    # policy that refuses to act.
    by_trigger = {
        kind: {"decisions": 0, "act": 0, "legal": 0, "act_and_legal": 0, "not_ready": 0}
        for kind in ("event", "wake", "recovery")
    }
    # Why a decision had nothing playable: affordability and placement are
    # separate gates, and the engine's own mask is what decides both.
    blocked = {"elixir_short": 0, "no_hand": 0, "slots_closed": 0, "no_legal_cell": 0, "other": 0}
    blocked_elixir: list[float] = []
    blocked_min_cost: list[float] = []
    blocked_samples: list[dict] = []
    cell_samples: list[dict] = []
    execution_mask_samples: list[dict] = []
    blocked_with_act = {"elixir_short": 0, "no_hand": 0, "slots_closed": 0, "no_legal_cell": 0, "other": 0}
    blocked_act_elixir: list[float] = []
    blocked_act_min_cost: list[float] = []
    channel_baseline = dict(getattr(coordinator, "profile", lambda: {})())
    started = time.perf_counter()
    timings = {"environment_step_seconds": 0.0, "feature_seconds": 0.0, "inference_seconds": 0.0}
    timings["deferred_observation_seconds"] = 0.0
    counters["observations_materialized"] = 0
    counters["inference_calls"] = 0
    tick = int(observations[0].tick)
    last_decision_tick = tick
    last_decision_by_owner = {0: tick, 1: tick}
    terminal = False

    try:
        while tick < max_ticks and time.perf_counter() - started < max_seconds:
            commands: dict[int, tuple] = {}
            gameplay_open = policy_actions_open(environment, tick)
            if not gameplay_open:
                counters["cancelled_gameplay_end"] += sum(plan is not None for plan in plans.values())
                for owner in (0, 1):
                    recorder.cancel(owner, reason="gameplay_end")
                plans = {0: None, 1: None}
                recovery_owners.clear()
            for owner in (0, 1):
                plan = plans[owner]
                if plan is None or tick < int(plan["scheduled_tick"]):
                    continue
                plans[owner] = None
                # Clear only after submission. Any validation failure below
                # requests a new decision for this actor at the next boundary.
                recovery_owners.add(owner)
                if not observations:
                    observation_started = time.perf_counter()
                    observations = environment.observe_dual(policy_features_only=True)
                    timings["deferred_observation_seconds"] += time.perf_counter() - observation_started
                    counters["observations_materialized"] += 1
                player = next((item for item in observations[owner].players if item.owner == owner), None)
                execution_mask = environment.action_mask(owner)
                if not mask_decision_ready(execution_mask):
                    recorder.cancel(owner, reason="decision_window_closed")
                    # The window closed between deciding and executing: an actor
                    # may only WAIT until its own pending play resolves, and the
                    # environment raises rather than silently dropping the plan.
                    counters["cancelled_window"] += 1
                    counters["rows_without_execution"] += 1
                    continue
                if plan.get("kind") == "activate_ability":
                    try:
                        action = ability_action_from_plan(
                            environment, observations[owner], owner, plan,
                            action_id=f"{match_id}:{ledger.submitted}:{owner}")
                    except ValueError:
                        recorder.cancel(owner, reason="ability_no_longer_legal")
                        counters["cancelled_ability"] += 1
                        counters["rows_without_execution"] += 1
                        continue
                    commands[owner] = (action,)
                    ledger.submit(action)
                    if owner in recorded_owners:
                        recorder.submit(owner, tick=int(plan["decided_tick"]), action_id=action.action_id)
                    recovery_owners.discard(owner)
                    continue
                if player is None or int(plan["card_id"]) not in player.hand:
                    recorder.cancel(owner, reason="card_left_hand")
                    # The state moved between deciding and executing; the engine
                    # would refuse, so the plan is excluded from training while
                    # its sampled decision remains in the audit trajectory.
                    counters["cancelled"] += 1
                    continue
                hand_index = next(slot for slot, card in native_hand_slots(player)
                                  if card == int(plan["card_id"]))
                closed = None
                if hand_index >= len(execution_mask.hand_slots) or not execution_mask.hand_slots[hand_index]:
                    closed = "hand_slot"
                elif not execution_mask.kinds.get("play_card", False):
                    closed = "play_kind"
                if closed is not None:
                    recorder.cancel(owner, reason=closed)
                    # Placements can remain legal while pending costs/selection
                    # close the slot. Keep the audit row but exclude this stale
                    # plan from the training lane before engine validation.
                    counters[f"cancelled_{closed}"] += 1
                    counters["rows_without_execution"] += 1
                    if len(execution_mask_samples) < 5:
                        execution_mask_samples.append({
                            "tick_decided": plan["decided_tick"], "tick_exec": tick,
                            "owner": owner, "card_id": int(plan["card_id"]),
                            "slot_decided": plan["hand_index"], "slot_now": hand_index,
                            "reason": closed,
                            "mask_reasons": {str(k): str(v) for k, v in execution_mask.reasons.items()},
                        })
                    continue
                if not _cell_is_legal(execution_mask, hand_index, plan["cell"]):
                    recorder.cancel(owner, reason="illegal_cell")
                    # Placement legality is evaluated at execution time too: the
                    # ring and the arena move on between deciding and acting, so
                    # a cell that was legal then can be refused now.
                    counters["cancelled_cell"] += 1
                    counters["rows_without_execution"] += 1
                    if len(cell_samples) < 5:
                        mask_now = execution_mask
                        entry = mask_now.placement_masks.get(str(hand_index))
                        rows_now = entry.get("row_major") if isinstance(entry, Mapping) else None
                        cell_samples.append(
                            {
                                "tick_decided": plan.get("decided_tick"),
                                "tick_exec": tick,
                                "owner": owner,
                                "card_id": int(plan["card_id"]),
                                "cell": [int(plan["cell"][0]), int(plan["cell"][1])],
                                "slot_decided": int(plan["hand_index"]),
                                "slot_now": hand_index,
                                "hand_decided": plan.get("hand_at_decision"),
                                "hand_now": [int(item) for item in player.hand],
                                "legal_cells_decided": plan.get("legal_cells_at_decision"),
                                "legal_cells_now": None
                                if not rows_now
                                else sum(1 for row_values in rows_now for value in row_values if value),
                                "cost_now": None,
                                "reasons": {str(k): str(v) for k, v in mask_now.reasons.items()},
                            }
                        )
                    continue
                if not spell_target_allowed(
                    observations[owner], owner, plan["card_id"], plan["cell"],
                    lightning_constraint=lightning_constraint, log_constraint=log_constraint,
                ):
                    # Preserve the sampled row and likelihood. This cancellation
                    # is an environment transition, not a replacement action.
                    spell = "log" if int(plan["card_id"]) == LOG_CARD_ID else "lightning"
                    recorder.cancel(owner, reason=f"{spell}_target")
                    counters[f"cancelled_{spell}_target"] += 1
                    counters["rows_without_execution"] += 1
                    continue
                try:
                    action = ActionV1.play(
                        owner,
                        hand_index,
                        plan["cell"],
                        card_id=int(plan["card_id"]),
                        execute_offset_ticks=1,
                        action_id=f"{match_id}:{ledger.submitted}:{owner}",
                    )
                except Exception:  # noqa: BLE001 - engine rejects a stale plan
                    recorder.cancel(owner, reason="action_construction_failed")
                    counters["cancelled"] += 1
                    continue
                commands[owner] = (action,)
                ledger.submit(action)
                if owner in recorded_owners:
                    recorder.submit(owner, tick=int(plan["decided_tick"]), action_id=action.action_id)
                counters["submissions_after_gameplay"] += int(not policy_actions_open(environment, tick))
                recovery_owners.discard(owner)

            if not commands:
                event_ticks = [when for when in event_ticks if when > tick]
                span = wait_span(
                    tick, plans.values(), event_ticks, last_decision_tick, max_advance_ticks, max_wait_ticks
                )
                if not gameplay_open:
                    span = max_advance_ticks
                elif recovery_owners:
                    span = 1
                if gameplay_open:
                    span = min(span, max(1, gameplay_end_tick(environment) - tick))
                commands = {owner: (ActionV1.wait(owner, ticks=span),) for owner in (0, 1)}

            commands = complete_event_commands(commands)
            before_elixir = raw_elixir(environment.raw_observation) if overflow is not None else None
            counters["steps"] += 1
            step_started = time.perf_counter()
            coordinator.begin_round((env_id,))
            try:
                observations, rewards, terms, truncs, _infos = environment.step(
                    commands, record_trace=False, policy_features_only=True,
                    **(dict(defer_observations=True, timing_profile=timings) if defer_observations else {})
                )
            except BaseException:
                coordinator.abort_round(RuntimeError("event RL rollout step failed"))
                raise
            else:
                coordinator.finish_round()

            previous_tick = tick
            timings["environment_step_seconds"] += time.perf_counter() - step_started
            tick = environment.current_tick if defer_observations else int(observations[0].tick)
            terminal = bool(terms["__all__"]) or bool(truncs["__all__"])
            # Resolve actual execution before advance can close terminal rows.
            outcomes = tuple(environment.action_outcomes())
            recovery_owners.update(ledger.reconcile(outcomes))
            recorder.reconcile(outcomes)
            penalties = None if overflow is None else overflow.advance(
                start=previous_tick, stop=tick, before=before_elixir,
                after=raw_elixir(environment.raw_observation), outcomes=outcomes,
                gameplay_before=gameplay_open, gameplay_after=policy_actions_open(environment, tick))
            recorder.advance(
                ticks=tick - previous_tick,
                rewards={owner: float(rewards.get(owner, 0.0)) for owner in (0, 1)},
                overflow_penalties=penalties,
                done=bool(terms["__all__"]),
                truncated=bool(truncs["__all__"]),
            )

            # Native execution, never a queued command, opens a new event for
            # both actors. The ledger deduplicates action IDs before this point.
            fresh = list(ledger.ability_events)
            counters["ability_events"] += len(ledger.ability_events)
            for owner in (0, 1):
                for item in environment.deployment_timing_events(owner):
                    event_id = str(item.get("nativeEventId", ""))
                    if not event_id or event_id in seen_events or str(item.get("kind")) != "card_play":
                        continue
                    seen_events.add(event_id)
                    fresh.append(item)
            if decision_observer is not None and fresh:
                decision_observer("native_events", tick=tick, events=tuple(fresh))
            if terminal:
                # The terminal transition is already recorded above. Deciding on
                # a finished episode would step an ended environment.
                break
            if not policy_actions_open(environment, tick):
                continue
            if fresh:
                counters["events"] += 1
            timed_out = {owner for owner in (0, 1) if tick - last_decision_by_owner[owner] >= max_wait_ticks}
            decision_owners = {0, 1} if fresh else timed_out | recovery_owners
            if not fresh and timed_out:
                counters["wake"] += 1
            if not decision_owners:
                continue

            if not observations:
                observation_started = time.perf_counter()
                observations = environment.observe_dual(policy_features_only=True)
                timings["deferred_observation_seconds"] += time.perf_counter() - observation_started
                counters["observations_materialized"] += 1
            for owner in (0, 1):
                if owner not in decision_owners:
                    continue
                trigger = "event" if fresh else "recovery" if owner in recovery_owners else "wake"
                feature_started = time.perf_counter()
                frame = canonicalize_event_observation(
                    tensorizers[owner].tensorize(observations[owner], validate=False)
                )
                legal = legal_event_actions(
                    environment,
                    observations[owner],
                    frame,
                    owner,
                    catalog=catalog,
                    card_count=model.card_count,
                    cell_count=model.cell_count,
                    board_width=width,
                    board_height=height,
                    delay_offset_ms=event_config.delay_offset_ms,
                    lightning_constraint=lightning_constraint,
                    log_constraint=log_constraint,
                    device=device,
                )
                # A short elixir bar does not make a play illegal: the contract
                # commits to a card and a delay, so the earliest *deliverable*
                # delay is the one that lands once elixir has accrued. Masking
                # below it keeps every sampled action executable, which is also
                # what makes the stored delay log-prob re-scorable later.
                # Legality already carries a per-card delay mask, so a short
                # elixir bar becomes a later deliverable tick rather than a
                # forbidden card. ACT is only unavailable when *no* card can be
                # delivered within the whole delay vocabulary.
                delay_mask = legal.delay_masks.unsqueeze(0)
                act_possible = bool(legal.act_allowed)
                supports_abilities = getattr(getattr(actors[owner], "config", None), "ability_actions", False)
                ability_possible = supports_abilities and bool(candidate_ability_mask(frame).any())
                if not (act_possible or ability_possible):
                    counters["act_impossible_states"] += 1
                gate_mask = torch.tensor([[True, act_possible]], dtype=torch.bool, device=device)
                timings["feature_seconds"] += time.perf_counter() - feature_started
                if not legal.decision_ready and not ignore_window:
                    if owner in recovery_owners:
                        continue  # Keep retrying when the actor's window opens.
                    # Keep tensorizer/tracker updates, but do not infer an action
                    # which this closed decision window would discard.
                    counters["not_ready"] += 1
                    by_trigger[trigger]["not_ready"] += 1
                    continue
                recovery_owners.discard(owner)
                last_decision_by_owner[owner] = tick
                last_decision_tick = min(last_decision_by_owner.values())
                if trigger == "recovery":
                    counters["recovery"] += 1
                inference_started = time.perf_counter()
                counters["inference_calls"] += 1
                decision_distribution = None
                if inference_client is not None:
                    if decision_observer is not None:
                        raise ValueError("decision_observer requires direct model inference")
                    client = inference_client if owner in recorded_owners else opponent_inference_client
                    if client is None:
                        raise ValueError("missing historical model inference route")
                    step = client.infer(
                        frame, owner=owner, tick=tick, gate_mask=gate_mask,
                        card_mask=legal.card_mask.unsqueeze(0), cell_masks=legal.cell_masks.unsqueeze(0),
                        delay_mask=delay_mask, temperatures=temperatures)
                else:
                    batch = frame.to_model_input(device, float_dtype=torch.float32)
                    with torch.inference_mode():
                        # Every collection path resets the recurrent state per
                        # decision; the update replays rows the same way.
                        actor = actors[owner]
                        context = actor.encode(
                            batch,
                            actor.initial_state(1, device=device),
                            episode_start=torch.ones(1, dtype=torch.bool, device=device),
                        )
                        if decision_observer is not None:
                            gate_logits, effective_gate_mask = actor.event_gate(context, gate_mask)
                            gate_probs = torch.softmax(
                                gate_logits.masked_fill(~effective_gate_mask, -torch.inf), dim=-1
                            )
                            card_probs = torch.softmax(
                                actor.card_logits(context).masked_fill(
                                    ~legal.card_mask.unsqueeze(0), -torch.inf
                                ),
                                dim=-1,
                            )
                            decision_distribution = {
                                "gate": gate_probs[0].detach().cpu().tolist(),
                                "card": card_probs[0].detach().cpu().tolist(),
                            }
                        step = actor.select_action(
                            context,
                            gate_mask=gate_mask,
                            card_mask=legal.card_mask.unsqueeze(0),
                            cell_masks=legal.cell_masks.unsqueeze(0),
                            delay_mask=delay_mask,
                            temperatures=temperatures,
                            **(dict(sample_uniforms=torch.rand((1, 4), generator=sampling_generator,
                                                               dtype=torch.float64).to(device))
                               if sampling_generator is not None else {}),
                        )
                if decision_observer is not None:
                    # Read-only diagnostics for native audits. These extra head
                    # distributions are computed after the sampled action and do
                    # not participate in selection or scheduling.
                    if decision_distribution is not None and bool(step.action.act[0]):
                        # The encoded context was created in inference mode above.
                        # Keep these read-only audit heads in the same mode so
                        # diagnostics cannot accidentally create an autograd graph.
                        with torch.inference_mode():
                            selected_card = step.action.card_row
                            selected_cell = step.action.cell
                            cell_logits = actor.cell_logits(context, selected_card)
                            cell_mask = actor._mask_for_card(
                                legal.cell_masks.unsqueeze(0), selected_card, label="cell_masks"
                            )
                            cell_probs = torch.softmax(
                                cell_logits.masked_fill(~cell_mask, -torch.inf), dim=-1
                            )
                            delay_dist = actor.action_delay_distribution(
                                context,
                                selected_card,
                                selected_cell,
                                step.action.ability_candidate,
                                delay_mask,
                                temperatures[3],
                            )
                            decision_distribution["cell"] = cell_probs[0].detach().cpu().tolist()
                            decision_distribution["delay"] = delay_dist.probs[0].detach().cpu().tolist()
                    decision_observer(
                        "decision",
                        owner=owner,
                        tick=tick,
                        trigger=trigger,
                        observation=observations[owner],
                        legal=legal,
                        step=step,
                        distribution=decision_distribution,
                    )
                timings["inference_seconds"] += time.perf_counter() - inference_started
                reason: str | None = None
                if not legal.act_allowed and not ability_possible:
                    reason, elixir, min_cost = _explain_blocked(environment, observations[owner], owner)
                    blocked[reason] += 1
                    if elixir is not None:
                        blocked_elixir.append(elixir)
                    if min_cost is not None:
                        blocked_min_cost.append(min_cost)
                    if len(blocked_samples) < 6:
                        mask = environment.action_mask(owner)
                        player = next(
                            (item for item in observations[owner].players if int(item.owner) == owner), None
                        )
                        blocked_samples.append(
                            {
                                "tick": tick,
                                "owner": owner,
                                "trigger": trigger,
                                "reason": reason,
                                "elixir": None if player is None else round(float(player.elixir_exact), 2),
                                "min_cost": min_cost,
                                "kinds": {str(key): bool(value) for key, value in mask.kinds.items()},
                                "hand_slots": [bool(slot) for slot in mask.hand_slots],
                                "reasons": {str(key): str(value) for key, value in mask.reasons.items()},
                                # 关键：被 insufficient_elixir 关掉的槽位还提供落点掩码吗？
                                "placement_slots": {
                                    str(slot): (
                                        None
                                        if not isinstance(mask.placement_masks.get(str(slot)), Mapping)
                                        else int(
                                            sum(
                                                1
                                                for row_values in (
                                                    mask.placement_masks[str(slot)].get("row_major") or []
                                                )
                                                for value in row_values
                                                if value
                                            )
                                        )
                                    )
                                    for slot in range(len(player.hand) if player is not None else 0)
                                },
                                "hand_costs": _hand_costs(environment, owner),
                            }
                        )
                counters["decisions"] += 1
                counters["decisions_after_gameplay"] += int(not policy_actions_open(environment, tick))
                slot = by_trigger[trigger]
                slot["decisions"] += 1
                slot["act"] += int(bool(step.action.act[0]))
                any_legal = bool(legal.act_allowed) or ability_possible
                slot["legal"] += int(any_legal)
                slot["act_and_legal"] += int(bool(step.action.act[0]) and any_legal)
                if reason is not None and bool(step.action.act[0]):
                    # The gate committed to a play while nothing was playable.
                    # Split by why nothing was playable.
                    blocked_with_act[reason] += 1
                    blocked_act_elixir.append(_player_elixir(observations[owner], owner))
                    blocked_act_min_cost.append(
                        _cheapest_hand_cost(environment, owner)
                    )
                # Frozen opponent decisions are not learner behaviour data.
                if overflow is not None:
                    overflow.start(owner, tick)
                if owner in recorded_owners:
                    recorder.decide(
                        owner=owner,
                        tick=tick,
                        frame=frame,
                        step=step,
                        card_mask=legal.card_mask.unsqueeze(0),
                        cell_masks=legal.cell_masks.unsqueeze(0),
                        delay_mask=delay_mask,
                        gate_mask=gate_mask,
                        tower_state=(tower_state(observations[owner].towers, initial_tower_hp)
                                     if reward_config is not None else None),
                    )
                if not bool(step.action.act[0]):
                    plans[owner] = None
                    continue
                if supports_abilities and int(step.action.ability_candidate[0]) >= 0:
                    plans[owner] = ability_plan(
                        frame, int(step.action.ability_candidate[0]), owner=owner, tick=tick,
                        ability_catalog=production_semantic_bundle().ability_catalog,
                        delay_ms=(event_config.bin_to_delay_ms(int(step.action.delay_bin[0]))
                                  if actors[owner].config.ability_delay else 0))
                    event_ticks.append(plans[owner]["scheduled_tick"] + 1)
                    counters["ability_decisions"] += 1
                    continue
                row = int(step.action.card_row[0])
                if not bool(delay_mask[0, row].any()):
                    recorder.cancel(owner, reason="no_legal_delay")
                    # Must stay zero: legality should have excluded this card
                    # because no delay in the vocabulary can deliver it.
                    counters["act_violation"] += 1
                    plans[owner] = None
                    continue
                if not legal.playable_now:
                    # Unaffordable right now, but this card's own delay floor
                    # lands after the bar fills, so the play is deliverable.
                    counters["deferred_for_elixir"] += 1
                player_now = next(
                    (item for item in observations[owner].players if item.owner == owner), None
                )
                delay_ms = int(event_config.bin_to_delay_ms(int(step.action.delay_bin[0])))
                cell_index = int(step.action.cell[0])
                plans[owner] = {
                    "card_id": int(legal.row_card_id[row]),
                    "hand_index": int(legal.hand_index[row]),
                    "hand_at_decision": [
                        int(item) for item in (player_now.hand if player_now is not None else [])
                    ],
                    "legal_cells_at_decision": int(legal.cell_masks[row].sum()),
                    # Plans are native; step.action above stays model-space.
                    "cell": event_cell_to_native(cell_index, owner=owner, board_width=width),
                    "delay_ms": delay_ms,
                    "decided_tick": tick,
                    "scheduled_tick": tick + max(1, math.ceil(delay_ms / TICK_MS)),
                }
                event_ticks.append(int(plans[owner]["scheduled_tick"]) + 1)
    finally:
        recorder.finish(truncated=not terminal)
        try:
            coordinator.close_session()
        except Exception:  # noqa: BLE001 - session teardown must not mask the run
            pass

    counters.update(ledger.counters())
    counters["confirmed_abilities"] = ledger.confirmed_abilities
    counters["confirmed_by_owner"] = {str(o): ledger.confirmed_by_owner[o] for o in (0, 1)}
    counters["recorded_owners"] = list(recorded_owners)
    for kind, slot in by_trigger.items():
        counters[f"not_ready_{kind}"] = slot["not_ready"]
        counters[f"act_share_{kind}"] = round(slot["act"] / max(1, slot["decisions"]), 4)
        counters[f"legal_share_{kind}"] = round(slot["legal"] / max(1, slot["decisions"]), 4)
        # Share of decisions where a play was possible at all, and - among those -
        # how often the gate actually committed to one.
        counters[f"act_given_legal_{kind}"] = round(slot["act_and_legal"] / max(1, slot["legal"]), 4)
        counters[f"act_with_no_legal_{kind}"] = slot["act"] - slot["act_and_legal"]
    counters["blocked"] = blocked
    counters["blocked_with_act"] = blocked_with_act
    if blocked_act_elixir:
        counters["blocked_act_elixir_mean"] = round(sum(blocked_act_elixir) / len(blocked_act_elixir), 3)
        counters["blocked_act_min_cost_mean"] = round(
            sum(blocked_act_min_cost) / len(blocked_act_min_cost), 3
        )
    counters["blocked_samples"] = blocked_samples
    counters["cell_samples"] = cell_samples
    counters["execution_mask_samples"] = execution_mask_samples
    if blocked_elixir:
        counters["blocked_elixir_mean"] = round(sum(blocked_elixir) / len(blocked_elixir), 3)
        counters["blocked_min_cost_mean"] = round(sum(blocked_min_cost) / len(blocked_min_cost), 3)
    counters["frames"] = {
        str(owner): recorder.lane_count(owner) for owner in recorded_owners
    }
    counters["results"] = recorder.results()
    if overflow is not None:
        from native_runner.training.event_rewards import reward_components
        counters["reward_config"] = reward_config
        counters["elixir_overflow"] = overflow.report()
        counters["reward_components"] = reward_components(
            [r for owner in recorded_owners for r in recorder.rows[owner]], reward_config)
        counters["tower_final_state"] = tower_state(
            environment._extract_towers(environment.raw_observation), initial_tower_hp)
        counters["tower_final_tick"] = environment.current_tick
    counters["action_constraints"] = action_constraints_contract(lightning_constraint, log_constraint)
    counters["terminal"] = terminal
    counters["elapsed_s"] = round(time.perf_counter() - started, 2)
    counters["channel_profile"] = dict(getattr(coordinator, "profile", lambda: {})())
    counters["channel_loop_profile"] = {
        key: value - channel_baseline.get(key, 0)
        for key, value in counters["channel_profile"].items()
        if isinstance(value, int) and not key.startswith("last_") and key != "connect_ns"
    }
    counters["timings"] = {k: round(v, 4) for k, v in timings.items()}
    counters["final_tick"] = tick
    return recorder, counters


def _player_elixir(observation, owner: int) -> float:
    player = next((item for item in observation.players if item.owner == owner), None)
    return 0.0 if player is None else float(player.elixir_exact)


def _hand_costs(environment, owner: int) -> list[float]:
    try:
        raw_hand = next(
            (item["hand"] for item in environment.raw_observation["players"] if int(item["owner"]) == int(owner)),
            [],
        )
        return [float(card["cost"]) for card in raw_hand]
    except Exception:  # noqa: BLE001 - diagnostics only
        return []


def _cheapest_hand_cost(environment, owner: int) -> float:
    try:
        raw_hand = next(
            (item["hand"] for item in environment.raw_observation["players"] if int(item["owner"]) == int(owner)),
            [],
        )
        costs = [float(card["cost"]) for card in raw_hand]
        return min(costs) if costs else 0.0
    except Exception:  # noqa: BLE001 - diagnostics only
        return 0.0


def _cell_is_legal(mask, hand_slot: int, cell) -> bool:
    """Is this placement still legal for this hand slot right now?"""

    entry = mask.placement_masks.get(str(int(hand_slot)))
    if not isinstance(entry, Mapping):
        return False
    rows = entry.get("row_major")
    if not rows:
        return False
    x, y = int(cell[0]), int(cell[1])
    if not 0 <= y < len(rows) or not 0 <= x < len(rows[y]):
        return False
    return bool(rows[y][x])


def _explain_blocked(environment, observation, owner: int) -> tuple[str, float | None, float | None]:
    """Classify why nothing was playable at this decision.

    The engine exposes two independent gates: which hand slots may be played and
    where each of them may be placed. Affordability shows up in the first one, so
    comparing the actor's elixir with the cheapest card it holds separates "cannot
    afford anything" from "nothing can be placed right now".
    """

    player = next((item for item in observation.players if int(item.owner) == int(owner)), None)
    if player is None or not player.hand:
        return "no_hand", None, None
    elixir = float(player.elixir_exact)
    min_cost: float | None = None
    try:
        raw_hand = next(
            (item["hand"] for item in environment.raw_observation["players"] if int(item["owner"]) == int(owner)),
            [],
        )
        costs = [float(card["cost"]) for card in raw_hand]
        if costs:
            min_cost = min(costs)
    except Exception:  # noqa: BLE001 - diagnostics only
        min_cost = None
    if min_cost is not None and elixir < min_cost - 1e-6:
        return "elixir_short", elixir, min_cost
    mask = environment.action_mask(owner)
    if not any(bool(slot) for slot in mask.hand_slots):
        return "slots_closed", elixir, min_cost
    if not bool(mask.kinds.get("play_card", False)):
        return "other", elixir, min_cost
    for slot in range(len(player.hand)):
        entry = mask.placement_masks.get(str(slot))
        if isinstance(entry, Mapping) and entry.get("row_major"):
            if any(any(row) for row in entry["row_major"]):
                return "other", elixir, min_cost
    return "no_legal_cell", elixir, min_cost


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--initialization-only", action="store_true",
                        help="explicitly collect finetuning data for a migrated checkpoint")
    parser.add_argument(
        "--opponent-checkpoint",
        type=Path,
        default=None,
        help="frozen snapshot for owner 1; omit for mirror self-play",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--deck0", default="golem-beatdown")
    parser.add_argument("--deck1", default="golem-beatdown")
    parser.add_argument("--matches", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20_260_916)
    parser.add_argument("--max-ticks", type=int, default=7_200)
    parser.add_argument("--max-seconds", type=float, default=900.0)
    parser.add_argument("--max-advance-ticks", type=int, default=300)
    parser.add_argument("--max-wait-ticks", type=int, default=130)
    parser.add_argument("--port", type=int, default=28189)
    parser.add_argument("--env-id", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--temperatures",
        default="1.0,1.0,1.0,1.0",
        help="gate,card,cell,delay sampling temperatures (recorded with every row)",
    )
    parser.add_argument(
        "--margin-beta",
        type=float,
        default=0.05,
        help="weight on the terminal win margin (crowns + weighted tower damage)",
    )
    parser.add_argument("--shards-per-file", type=int, default=1)
    parser.add_argument(
        "--ignore-decision-window",
        action="store_true",
        help="diagnostic: decide even when the engine says the actor is not ready",
    )
    add_lightning_target_arguments(parser)
    add_log_target_arguments(parser)
    args = parser.parse_args()
    lightning_constraint = lightning_constraint_from_args(args)
    log_constraint = log_constraint_from_args(args)

    temperatures = tuple(float(item) for item in args.temperatures.split(","))
    if len(temperatures) != 4:
        raise SystemExit("--temperatures needs four comma-separated values")

    decks_by_name = _deck_registry()
    decks = {0: tuple(decks_by_name[args.deck0]), 1: tuple(decks_by_name[args.deck1])}
    device = torch.device(args.device)
    loaded = load_event_checkpoint(args.checkpoint, device=device, initialization_only=args.initialization_only)
    model = loaded.model
    model.eval()
    event_config = model.event_config
    policy_version = str(loaded.payload.get("policy_version") or args.checkpoint.stem)
    print(json.dumps({"event": "checkpoint-loaded", "role": "learner",
                      "checkpoint_sha256": loaded.checkpoint_sha256,
                      "contract_sha256": loaded.contract_sha256,
                      "snapshot_only": model.backbone.config.snapshot_only}, sort_keys=True), flush=True)

    opponent_model = None
    if args.opponent_checkpoint is not None:
        opponent_loaded = load_event_checkpoint(args.opponent_checkpoint, device=device,
                                                initialization_only=args.initialization_only)
        opponent_model = opponent_loaded.model
        opponent_model.eval()
        print(json.dumps({"event": "checkpoint-loaded", "role": "opponent",
                          "checkpoint_sha256": opponent_loaded.checkpoint_sha256,
                          "contract_sha256": opponent_loaded.contract_sha256,
                          "snapshot_only": opponent_model.backbone.config.snapshot_only}, sort_keys=True), flush=True)
        if opponent_model.event_config != event_config:
            raise ValueError("learner and opponent must use the same event action contract")

    bundle = production_semantic_bundle()
    proxies, coordinator = build_resident_training_proxies_v4(
        (args.env_id,),
        host="127.0.0.1",
        port=args.port,
        timeout=90.0,
        step_until_event=True,
    )
    proxy = proxies[0]
    execution_ruleset_id = hashlib.sha256(
        f"event-rl-rollout:{args.deck0}:{args.deck1}:{args.seed}".encode("utf-8")
    ).hexdigest()
    environment = BattleEnvV1(
        native=proxy,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
        ruleset_id=execution_ruleset_id,
        # Without this the reward is exactly +/-1 and the margin never appears:
        # every win scores the same, comfortable or narrow.
        terminal_margin_beta=args.margin_beta,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    contract = default_event_contract(event_config)
    contract["policy_version"] = policy_version
    contract["temperatures"] = list(temperatures)
    contract["behavior_checkpoint_sha256"] = loaded.checkpoint_sha256
    contract["behavior_model_contract_hash"] = loaded.contract_sha256
    contract["action_constraints"] = action_constraints_contract(lightning_constraint, log_constraint)
    print(json.dumps({"event": "action-constraints", **contract["action_constraints"]}, sort_keys=True), flush=True)

    written = 0
    for index in range(args.matches):
        match_id = f"{policy_version}-{args.seed + index:08d}"
        recorder, counters = play_match(
            environment=environment,
            coordinator=coordinator,
            model=model,
            opponent_model=opponent_model,
            event_config=event_config,
            catalog=model.backbone.catalog,
            device=device,
            decks=decks,
            match_id=match_id,
            policy_version=policy_version,
            seed=args.seed + index,
            env_id=args.env_id,
            temperatures=temperatures,
            max_ticks=args.max_ticks,
            max_seconds=args.max_seconds,
            max_advance_ticks=args.max_advance_ticks,
            max_wait_ticks=args.max_wait_ticks,
            ignore_window=args.ignore_decision_window,
            first_match=(index == 0),
            behavior_checkpoint_sha256=loaded.checkpoint_sha256,
            behavior_model_contract_hash=loaded.contract_sha256,
            lightning_constraint=lightning_constraint,
            log_constraint=log_constraint,
        )
        # Against a frozen opponent only this policy's perspective is behaviour
        # data: the opponent's rows came from another policy entirely, so their
        # log-probs could not be re-scored under this one.
        recorded_owners = (0,) if opponent_model is not None else (0, 1)
        rows = sum(recorder.lane_count(owner) for owner in recorded_owners)
        if rows:
            shard_index = written // max(1, args.shards_per_file)
            destination = args.out_dir / f"part-00-shard-{shard_index:06d}.crev.zst"
            recorder.to_shard(
                destination, shard_id=destination.stem, contract=contract, owner=None if opponent_model is None else 0
            )
            written += 1
        print(
            json.dumps(
                {"match": match_id, "rows": rows, "opponent_file": None if opponent_model is None
                 else args.opponent_checkpoint.name, **counters},
                sort_keys=True,
            ),
            flush=True,
        )
    print(json.dumps({"event": "rollout-done", "matches": args.matches, "shards": written}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
