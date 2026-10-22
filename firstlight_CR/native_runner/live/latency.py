"""Measure decision-to-click separately from command execution and deployment.

Usage: python -m native_runner.live.latency artifacts/live-web/jobs/JOB
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics


def read_rows(path):
    if not path.exists():
        return []
    content = path.read_bytes()
    complete = content[:content.rfind(b"\n") + 1]
    return [json.loads(line) for line in complete.splitlines() if line.strip()]


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return dict(n=0)
    return dict(n=len(ordered), median=round(statistics.median(ordered), 3),
                p95=round(ordered[math.ceil(len(ordered)*.95)-1], 3),
                min=round(ordered[0], 3), max=round(ordered[-1], 3))


def device_on_host(device_ns, sync):
    """Host time interval, NOT an assertion that guest and host clocks agree."""
    return (int(device_ns) + sync["host_minus_device_min_ns"],
            int(device_ns) + sync["host_minus_device_max_ns"])


def unconfirmed_diagnostics(request, target, failed, markers, snapshots):
    """Describe evidence, without granting permission to resend an input."""
    card = request["before"]["own"]["hand"][request["slot"]]["card"]
    end_ns = failed.get("monotonic_ns") if failed else None
    candidates = [r for r in markers if r["stage"] == "own_command_created"
        and r.get("battle_epoch") == request["epoch"]
        and r.get("card_id") == request["card_id"] and r.get("deck_slot") == card["deck_slot"]
        and int(r["device_monotonic_ns"]) >= int(target["device_down_before_ns"])
        and (end_ns is None or device_on_host(r["device_monotonic_ns"], target["clock_sync"])[0] <= end_ns)]
    result = dict(command_candidates=len(candidates), command_status="missing_or_ambiguous")
    if len(candidates) == 1:
        command = candidates[0]
        related = [r for r in markers if r.get("command_id") == command["command_id"]
                   and r.get("battle_epoch") == request["epoch"]]
        returns = [r for r in related if r["stage"] == "own_command_submit_returned"]
        outbound = any(r["stage"] == "own_command_outbound" for r in related)
        result.update(command_id=command["command_id"], command_tick=command["tick"],
                      outbound_observed=outbound)
        state = command.get("submit_state", {})
        result["submit_state"] = state
        if "battle_sync" in command:
            result["battle_sync"] = command["battle_sync"]
        if len(returns) == 1:
            returned = returns[0]
            result.update(accepted=returned.get("accepted"),
                          validations=returned.get("validations", []))
            # Historical false returns do NOT distinguish rejection/queueing.
            rejected = (returned.get("outcome") == "validation_rejected"
                        and returned.get("command_destroyed") is True
                        and returned.get("outbound_seen") is False
                        and returned.get("accepted") is False and not outbound)
            sync_stopped = (state.get("battle_stopped") == 1
                            and state.get("defer_enabled") == 0 and state.get("queued_commands") == 0
                            and returned.get("command_destroyed") is True
                            and returned.get("outbound_seen") is False
                            and returned.get("accepted") is False and returned.get("validations") == []
                            and not outbound)
            result["command_status"] = ("battle_sync_stopped" if sync_stopped else
                "validation_rejected" if rejected else
                "outbound_without_receipt" if outbound else
                "not_accepted_or_locally_queued" if returned.get("accepted") is False else
                "accepted_without_receipt")
            if sync_stopped:
                prior_sync = [r for r in markers
                    if r.get("battle_epoch") == request["epoch"]
                    and r["stage"] in ("battle_sync_fault", "battle_sync_state")
                    and int(r["device_monotonic_ns"]) <= int(command["device_monotonic_ns"])]
                cleared = max((int(r["device_monotonic_ns"]) for r in prior_sync
                    if r.get("battle_sync", {}).get("stopped") == 0), default=-1)
                faults = [r for r in prior_sync if r["stage"] == "battle_sync_fault"
                          and int(r["device_monotonic_ns"]) > cleared]
                if faults:
                    result["sync_fault"] = max(faults, key=lambda r: int(r["device_monotonic_ns"]))
    if end_ns is not None:
        prior = [r for r in snapshots if r.get("battle_epoch") == request["epoch"]
                 and r.get("received_monotonic_ns", end_ns + 1) <= end_ns]
        if prior:
            last = max(prior, key=lambda r: r["received_monotonic_ns"])
            age = (end_ns - last["received_monotonic_ns"]) / 1e6
            result.update(last_snapshot_tick=last["tick"], last_snapshot_age_ms=round(age, 3),
                          snapshot_stale=age > 500, ended=last.get("ended"))
    return result


def analyze(directory):
    directory = Path(directory)
    capture = read_rows(directory / "capture.jsonl")
    policy = read_rows(directory / "policy.jsonl")
    markers = [r for r in capture if r.get("event") == "latency_marker"]
    snapshots = [r for r in capture if r.get("event") == "snapshot"]
    confirmations = {Path(r["receipt"]).name: r for r in policy if r.get("event") == "confirmed"}
    metrics = defaultdict(list)
    actions, problems, cancelled, terminal_unconfirmed = [], [], [], []
    for path in sorted(directory.glob("policy-touch-*.jsonl")):
        events = {r["event"]: r for r in read_rows(path)}
        request = events.get("requested")
        target = events.get("target_touch_completed", {}).get("input_timing")
        hand = events.get("hand_touch_completed", {}).get("input_timing")
        decision = request.get("decision_timing") if request else None
        if "cancelled_before_target" in events and target is None:
            cancelled.append(dict(touch=path.name, reason=events["cancelled_before_target"].get("reason")))
            continue
        if not (decision and hand and target):
            problems.append(dict(touch=path.name, reason="missing precise decision/touch timing"))
            continue
        finished = decision["decision_finished_monotonic_ns"]
        down = int(target["device_down_before_ns"])
        up = int(target["device_up_before_ns"])
        bounds = device_on_host(down, target["clock_sync"])
        up_bounds = device_on_host(up, target["clock_sync"])
        hand_bounds = device_on_host(hand["device_down_before_ns"], hand["clock_sync"])
        midpoint = (bounds[0] + bounds[1])/2
        action = dict(touch=path.name, decision_id=decision["decision_id"],
            card_id=request["card_id"], selected_delay_ms=decision["selected_delay_ms"],
            source_tick=decision["source_tick"], scheduled_tick=decision["scheduled_tick"],
            policy_scheduled_tick=decision.get("policy_scheduled_tick", decision["scheduled_tick"]),
            input_lead_ms=decision.get("input_lead_ms", 0),
            scheduled_delay_ms=decision.get("scheduled_delay_ms",
                (decision["scheduled_tick"]-decision["source_tick"])*50),
            due_observed_tick=decision["due_observed_tick"],
            decision_finished_host_ns=finished, target_down_device_ns=down,
            target_up_device_ns=up, target_down_host_bounds_ns=bounds,
            decision_to_target_down_ms=(midpoint-finished)/1e6,
            decision_to_target_down_bounds_ms=[(t-finished)/1e6 for t in bounds],
            decision_to_target_up_ms=((up_bounds[0]+up_bounds[1])/2-finished)/1e6,
            decision_to_hand_down_ms=((hand_bounds[0]+hand_bounds[1])/2-finished)/1e6,
            decision_to_dispatch_ms=(decision["dispatch_monotonic_ns"]-finished)/1e6,
            dispatch_to_target_down_ms=(midpoint-decision["dispatch_monotonic_ns"])/1e6,
            hand_down_to_target_down_ms=(down-int(hand["device_down_before_ns"]))/1e6,
            target_down_injection_ms=(int(target["device_down_after_ns"])-down)/1e6,
            target_up_injection_ms=(int(target["device_up_after_ns"])-up)/1e6,
            clock_sync_uncertainty_ms=(bounds[1]-bounds[0])/2e6,
            timing_status="touch_measured")
        confirmation = confirmations.get(path.name)
        if confirmation:
            receipt = confirmation["native_receipt"]
            same_receipt = [r for r in markers if r.get("battle_epoch") == request["epoch"]
                and r.get("public_sequence") == receipt["sequence"]
                and r.get("card_id") == request["card_id"]]
            consumed = [r for r in same_receipt if r["stage"] == "own_native_consumed"]
            entering = [r for r in same_receipt if r["stage"] == "own_native_consume_enter"]
            if len(consumed) == len(entering) == 1:
                enter, end = entering[0], consumed[0]
                consumed_ns = int(enter["device_monotonic_ns"])
                deck_slot = request["before"]["own"]["hand"][request["slot"]]["card"]["deck_slot"]
                candidates = [r for r in markers if r["stage"] == "own_command_created"
                    and r["battle_epoch"] == request["epoch"] and r["card_id"] == request["card_id"]
                    and r.get("deck_slot") == deck_slot
                    and down <= int(r["device_monotonic_ns"]) <= consumed_ns]
                if len(candidates) == 1:
                    command = candidates[0]
                    created = int(command["device_monotonic_ns"])
                    returned = [r for r in markers if r["stage"] == "own_command_submit_returned"
                        and r.get("command_id") == command["command_id"] and r.get("accepted") is True]
                    if len(returned) == 1 and consumed_ns >= created:
                        action.update(timing_status="command_and_consumption_measured",
                            command_id=command["command_id"], command_created_tick=command["tick"],
                            command_created_device_ns=created, consumption_tick=enter["tick"],
                            consumption_device_ns=consumed_ns, public_sequence=receipt["sequence"],
                            target_down_to_command_created_ms=(created-down)/1e6,
                            target_up_to_command_created_ms=(created-up)/1e6,
                            command_created_to_consumption_ms=(consumed_ns-created)/1e6,
                            target_down_to_consumption_ms=(consumed_ns-down)/1e6,
                            consumption_function_ms=(int(end["device_monotonic_ns"])-consumed_ns)/1e6)
                    else:
                        problems.append(dict(touch=path.name, reason="command not uniquely accepted"))
                else:
                    problems.append(dict(touch=path.name, reason="command association missing or ambiguous", candidates=len(candidates)))
            else:
                problems.append(dict(touch=path.name, reason="consumption marker missing or duplicated"))
            # Appearance/deployment are sampled public facts, never called
            # exact native boundaries or substituted for a missing marker.
            before_ids = {e["id"] for e in request["before"]["entities"]}
            appearances = []
            for snapshot in snapshots:
                if snapshot["battle_epoch"] != request["epoch"] or not receipt["tick"] <= snapshot["tick"] <= receipt["tick"]+4:
                    continue
                for entity in snapshot["entities"]:
                    if (entity["id"] not in before_ids and entity["owner"] == snapshot["own_side"]
                            and entity["card_id"] == request["card_id"]):
                        before_ids.add(entity["id"])
                        phase = entity.get("phase") or {}
                        appearances.append(dict(entity_id=entity["id"], first_seen_tick=snapshot["tick"],
                            deploy_remaining_ms=phase.get("deploy_remaining_ms"),
                            configured_deploy_time_ms=phase.get("configured_deploy_time_ms")))
            for appearance in appearances:
                last_deploying_tick = None
                for snapshot in snapshots:
                    if snapshot["battle_epoch"] != request["epoch"] or snapshot["tick"] < appearance["first_seen_tick"]:
                        continue
                    entity = next((e for e in snapshot["entities"] if e["id"] == appearance["entity_id"]), None)
                    remaining = ((entity.get("phase") or {}).get("deploy_remaining_ms")
                                 if entity else None)
                    if remaining is not None and remaining > 0:
                        last_deploying_tick = snapshot["tick"]
                    elif remaining == 0:
                        if last_deploying_tick is not None:
                            appearance["deployment_finished_tick_bounds"] = [last_deploying_tick, snapshot["tick"]]
                        break
            action["sampled_entity_appearances"] = appearances
        else:
            ended_touch = events.get("unconfirmed_at_terminal")
            diagnostic = unconfirmed_diagnostics(request, target, events.get("unconfirmed") or ended_touch, markers, snapshots)
            action["unconfirmed_diagnostics"] = diagnostic
            if ended_touch:
                action["timing_status"] = "battle_ended_before_confirmation"
                terminal_unconfirmed.append(dict(touch=path.name, terminal=ended_touch["terminal"], **diagnostic))
            else:
                problems.append(dict(touch=path.name, reason="touch has no native confirmation", **diagnostic))
        for key, value in action.items():
            if key.endswith("_ms") and isinstance(value, (float, int)):
                metrics[key].append(value)
        if action["selected_delay_ms"] == 100:
            metrics["minimum_delay_decision_to_target_down_ms"].append(action["decision_to_target_down_ms"])
        actions.append(action)
    result = dict(schema="cr-live-latency-report.v1", job=directory.name, actions=actions,
        metrics={key:distribution(values) for key,values in metrics.items()}, problems=problems,
        sync_faults=[r for r in markers if r["stage"] == "battle_sync_fault"],
        terminal_unconfirmed=terminal_unconfirmed,
        cancelled_before_target=cancelled,
        timing_errors=[r for r in capture if r.get("event") == "timing_error"],
        notes=["Decision completion is after policy.infer returns; selected model delay is retained separately.",
               "scheduled_tick is the compensated click deadline; policy_scheduled_tick retains the original deadline.",
               "Android timestamps are CLOCK_MONOTONIC; host conversion uses measured offset bounds.",
               "Device DOWN/UP boundaries bracket InputManager injection, not screen presentation.",
               "own_command_created means fully constructed local command entering c893d0, not allocation.",
               "Native consumption timestamps are separate from sampled appearance/deployment timers.",
               "Missing or ambiguous command markers are not replaced with estimates."])
    return result


def write_report(directory):
    result = analyze(directory)
    destination = Path(directory) / "latency.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = write_report(args.directory)
    print(json.dumps({key:report[key] for key in ("job", "metrics", "problems", "timing_errors")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
