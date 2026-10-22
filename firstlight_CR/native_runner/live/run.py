"""One experimental real-client match with partial inputs and normal touches.

Default is shadow inference. Execution requires an explicit partial-input flag,
measured touch calibration and recognized public tower identities.
This is an integration diagnostic, not a policy-strength evaluation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

from .capture import DEFAULT_ADB
from .control import device_lease
from .policy import PartialLivePolicy
from .projection import TouchProjection
from .snapshot import LiveStateError, latest_snapshot, validate_touch
from .touch import deploy_once, TouchSession, TouchTransaction, TouchCancelled, TouchEnded
from .events import event_watermark
from .scheduler import DEFAULT_INPUT_LEAD_MS, LiveScheduler, SCHEDULER_VERSION
from ..training.event_spell_constraints import spell_target_allowed, action_constraints_contract


def plan_is_current(row: dict, plan: dict, policy: PartialLivePolicy, *, adapter=None) -> bool:
    card = validate_touch(row, epoch=plan["battle_epoch"], slot=plan["slot"], card_id=plan["card_id"])
    if row["own_side"] != plan["own_side"] or row["own"]["elixir_raw"] < card["cost"] * 10000:
        return False
    if (card["form_code"] != plan.get("form_code", 0)
            or card["cost"] != plan.get("effective_cost", card["cost"])):
        return False
    observation = (adapter or policy.adapter).observation(row)
    mask = observation.action_mask
    entry = mask.placement_masks.get(str(plan["slot"]))
    x, y = plan["native_cell"]
    return bool(entry and mask.hand_slots[plan["slot"]] and entry["row_major"][y][x]
                and spell_target_allowed(observation, row["own_side"], plan["card_id"], (x, y),
                                         lightning_constraint=policy.lightning, log_constraint=policy.log))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--calibration", type=Path, nargs="+", required=True)
    parser.add_argument("--seconds", type=float, default=420)
    parser.add_argument("--seed", type=int, help="uint32 policy sampling seed; random by default")
    parser.add_argument("--input-lead-ms", type=int, default=DEFAULT_INPUT_LEAD_MS,
                        help="advance click deadlines by this many ms (multiple of 50; 0 disables)")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--public-tower-identities", "--verified-princess-towers",
                        dest="public_tower_identities", action="store_true",
                        help="require exact public tower identities; legacy flag remains an alias")
    args = parser.parse_args()
    if not args.allow_partial or not args.public_tower_identities:
        parser.error("explicit partial-input acceptance and public tower identities are required")
    if args.seconds <= 0:
        parser.error("seconds must be positive")
    if args.input_lead_ms < 0 or args.input_lead_ms % 50:
        parser.error("input lead must be a nonnegative multiple of 50 ms")
    with device_lease(args.serial):
        try:
            run_match(args)
        finally:
            # Diagnostics must not change the match's outcome or retry input.
            if args.output.exists():
                from .latency import write_report
                try:
                    write_report(args.output.parent)
                except Exception as exc:
                    print(json.dumps({"event": "latency_report_error", "reason": str(exc)}), flush=True)


def run_match(args):
    projection = TouchProjection.from_files(args.calibration)
    policy = PartialLivePolicy(args.checkpoint, seed=getattr(args, "seed", None))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    counts = {"decisions": 0, "submitted": 0, "confirmed": 0, "cancelled": 0}
    bound_epoch = None
    previous_sequence = -1
    last_error = None
    start = time.monotonic()
    blocked_since = None
    scheduler = LiveScheduler(max_wait_ticks=policy.model.event_config.max_wait_ticks,
                              input_lead_ms=getattr(args, "input_lead_ms", DEFAULT_INPUT_LEAD_MS))
    session = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-touch")
    flight = None
    touch_attempt = 0
    with args.output.open("x") as output:
        def record(event, **values):
            row = {"event": event, "monotonic_ns": time.monotonic_ns(), **values}
            encoded = json.dumps(row, default=list, allow_nan=False)
            output.write(encoded + "\n")
            output.flush()
            print(encoded, flush=True)

        try:
            if args.execute:
                session = TouchSession(adb=DEFAULT_ADB, serial=args.serial, capture=args.capture,
                                       log_dir=args.output.parent)
            record("ready", checkpoint_sha256=policy.loaded.checkpoint_sha256, execute=args.execute,
                   action_constraints=action_constraints_contract(policy.lightning, policy.log),
                   partial_input=True, calibration_residual_px=projection.max_residual_px,
                   scheduler_version=SCHEDULER_VERSION, sampling_seed=policy.seed,
                   input_lead_ms=scheduler.input_lead_ms,
                   temperatures=[1., 1., 1., 1.], input_transport="persistent-android-input.v1",
                   limitations=["incomplete_runtime", "unknown_card_levels", "approximate_touch_projection",
                                "incomplete_tower_runtime", "incomplete_ability_and_spell_runtime"])
            while time.monotonic() - start < args.seconds:
                time.sleep(0.01)
                try:
                    if flight is not None:
                        future, transaction, plan, receipt = flight
                        if transaction.committed and not plan.get("submitted_logged"):
                            counts["submitted"] += 1
                            plan["submitted_logged"] = True
                            record("submitted", source_tick=plan["tick"], scheduled_tick=plan["scheduled_tick"],
                                   card_id=plan["card_id"], counts=dict(counts))
                        if future.done():
                            flight = None
                            if transaction.committed and not plan.get("submitted_logged"):
                                counts["submitted"] += 1
                                plan["submitted_logged"] = True
                                record("submitted", source_tick=plan["tick"], card_id=plan["card_id"], counts=dict(counts))
                            try:
                                result = future.result()
                            except TouchEnded as exc:
                                terminal = exc.after
                                if (not transaction.committed or terminal.get("ended") is not True
                                        or terminal.get("battle_epoch") != bound_epoch
                                        or terminal.get("own_side") != plan["own_side"]
                                        or terminal.get("tick", -1) < plan["tick"]):
                                    raise RuntimeError(f"unconfirmed committed touch: {exc}") from exc
                                # Preserve the missing receipt without inventing a
                                # successful play or discarding this battle's result.
                                record("unconfirmed_at_terminal", tick=terminal["tick"],
                                       card_id=plan["card_id"], receipt=str(receipt),
                                       reason=str(exc), counts=dict(counts))
                                record("terminal", tick=terminal["tick"], winner=terminal["winner"],
                                       own_side=terminal["own_side"], same_battle=True, counts=dict(counts))
                                return
                            except (LiveStateError, ValueError) as exc:
                                if transaction.committed:
                                    raise RuntimeError(f"unconfirmed committed touch: {exc}") from exc
                                counts["cancelled"] += 1
                                record("cancelled", reason=str(exc), plan=plan, counts=dict(counts))
                            else:
                                counts["confirmed"] += 1
                                bound_epoch = result.get("after_epoch", bound_epoch)
                                record("confirmed", tick=result["after_tick"], source_tick=plan["tick"],
                                       scheduled_tick=plan["scheduled_tick"], card_id=plan["card_id"],
                                       native_cell=plan["native_cell"], receipt=str(receipt),
                                       target_touch_tick=result["target_touch_tick"], touch_sent_tick=result["touch_sent_tick"],
                                       native_receipt=result["native_receipt"], hand_touch_ms=result["hand_touch_ms"],
                                       target_touch_ms=result["target_touch_ms"], counts=dict(counts))
                            scheduler.recover()
                    row = latest_snapshot(args.capture)
                    if bound_epoch is not None and (row["ended"] or row["battle_epoch"] != bound_epoch):
                        if flight is not None:
                            flight[1].cancel("battle ended before deployment")
                            continue  # Resolve the owned in-flight receipt first.
                        record("terminal", tick=row["tick"], winner=row["winner"], own_side=row["own_side"],
                               same_battle=row["battle_epoch"] == bound_epoch, counts=counts)
                        return
                    age = (time.monotonic_ns() - row["received_monotonic_ns"]) / 1e6
                    if not 0 <= age <= 500:
                        raise LiveStateError("live snapshot is older than 500 ms")
                    if row["ended"]:
                        continue
                    if row["sequence"] == previous_sequence:
                        continue
                    previous_sequence = row["sequence"]
                    fresh = policy.observe(row)
                    if row["tick"] < 150:
                        continue
                    if bound_epoch is None:
                        bound_epoch = row["battle_epoch"]
                        blocked_since = None
                        record("bound", epoch=bound_epoch, tick=row["tick"], own_side=row["own_side"])
                    cancelled = scheduler.observe(row, fresh)
                    if fresh:
                        record("public_card_plays", tick=row["tick"], plays=fresh)
                        if flight is not None:
                            flight[1].cancel("new public card play before deployment")
                    if cancelled is not None:
                        counts["cancelled"] += 1
                        record("cancelled", reason="public_card_play", tick=row["tick"], plan=cancelled,
                               counts=dict(counts))
                    if flight is not None:
                        continue  # Observe events while the touch worker awaits its receipt.
                    plan = scheduler.take_due(row["tick"])
                    if plan is not None:
                        if not args.execute:
                            record("shadow_due", tick=row["tick"], plan=plan)
                            scheduler.recover()
                        else:
                            touch_attempt += 1
                            receipt = args.output.with_name(args.output.stem + f"-touch-{touch_attempt:03d}.jsonl")
                            transaction = TouchTransaction()
                            guard_adapter = policy.adapter.fork()

                            def guard(latest, frozen_plan=plan, adapter=guard_adapter):
                                if event_watermark(latest) != frozen_plan["event_watermark"]:
                                    raise TouchCancelled("new public card play before deployment")
                                if not plan_is_current(latest, frozen_plan, policy, adapter=adapter):
                                    raise TouchCancelled("planned card or target is no longer legal")

                            timing = {**plan.get("timing", {}), "source_tick": plan["tick"],
                                "scheduled_tick": plan["scheduled_tick"], "selected_delay_ms": plan["delay_ms"],
                                "policy_scheduled_tick": plan["policy_scheduled_tick"],
                                "input_lead_ms": plan["input_lead_ms"], "scheduled_delay_ms": plan["scheduled_delay_ms"],
                                "due_observed_tick": row["tick"], "due_snapshot_received_ns": row["received_monotonic_ns"],
                                "dispatch_monotonic_ns": time.monotonic_ns()}
                            future = executor.submit(deploy_once, adb=DEFAULT_ADB, serial=args.serial,
                                capture=args.capture, receipt=receipt, epoch=bound_epoch, slot=plan["slot"],
                                card_id=plan["card_id"], screen=tuple(plan["screen"]), session=session,
                                transaction=transaction, guard=guard, decision_timing=timing)
                            flight = (future, transaction, plan, receipt)
                            continue
                    if scheduler.trigger is None:
                        continue
                    decision_started = time.monotonic_ns()
                    decision = policy.infer(row)
                    decision["timing"] = dict(schema="cr-live-decision-timing.v1",
                        decision_id=f"{row['battle_epoch']}:{row['sequence']}",
                        source_snapshot_received_ns=row["received_monotonic_ns"],
                        decision_started_monotonic_ns=decision_started,
                        decision_finished_monotonic_ns=time.monotonic_ns())
                    decision["trigger"] = scheduler.trigger
                    counts["decisions"] += 1
                    last_error = None
                    blocked_since = None
                    if decision["act"]:
                        # Coarse border cells outside the calibrated viewport are
                        # withheld, with no clipping to an unintended placement.
                        decision["screen"] = projection.screen(tuple(decision["native_cell"]), row["own_side"],
                                                                model_subcell_offset=decision.get("model_subcell_offset"))
                    scheduler.decide(decision)
                    decision["timing"]["plan_installed_monotonic_ns"] = time.monotonic_ns()
                    record(**decision)
                except (LiveStateError, ValueError) as exc:
                    # Unconfirmed touches are never retried automatically.
                    if flight is not None:
                        flight[1].cancel("capture or observation became invalid")
                    if scheduler.pending is not None:
                        counts["cancelled"] += 1
                        record("cancelled", reason="capture_or_observation_error", plan=scheduler.pending,
                               counts=dict(counts))
                        scheduler.pending = None
                    message = str(exc)
                    if message != last_error:
                        record("blocked", reason=message)
                    last_error = message
                    blocked_since = blocked_since or time.monotonic()
                    if bound_epoch is not None and time.monotonic()-blocked_since > 15:
                        raise RuntimeError("live input blocked for 15 seconds: " + message) from exc
                    scheduler.recover()
        except KeyboardInterrupt:
            record("interrupted", counts=counts)
        except Exception as exc:
            record("failed", reason=str(exc), counts=counts)
            raise
        else:
            record("timeout", counts=counts)
        finally:
            if flight is not None:
                flight[1].cancel("policy runner stopped")
            executor.shutdown(wait=True, cancel_futures=True)
            if session is not None:
                session.close()


if __name__ == "__main__":
    main()
