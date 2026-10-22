import argparse
import json
from pathlib import Path
import sys
import time
import torch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.native import FinalBattleEnv, FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.timing import Reservation
from native_runner.training.fair_final.adapter import native_payment_mask
from native_runner.training.fair_final.adapter import source_policy
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.candidates import structured_candidates
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.transport import create_search_native
from native_runner.training.fair_search.contracts import semantic_digest
from dataclasses import fields


def choose(environment, owner, delay):
    mask = environment.action_mask(owner)
    player = next(player for player in environment.raw_observation["players"] if player["owner"] == owner)
    for card in player["hand"]:
        slot = int(card["handIndex"])
        if not mask.hand_slots[slot]:
            continue
        entry = mask.placement_masks[str(slot)]
        for row, cells in enumerate(entry["row_major"]):
            for column, legal in enumerate(cells):
                if legal:
                    cell = row * 18 + column
                    return FullAction(int(card["cardId"]), cell if owner == 0 else 575 - cell, delay)
    raise ValueError("probe needs an affordable legal opening play")


def run_case(native, *, seed, same_tick, sidecar_factory):
    environment = FinalBattleEnv(native=native)
    environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
        seed=seed, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
        options={"render_mode": "headless", "decision_ticks": 1,
            "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
    native.start_atomic_steps()
    runtime = FinalDecisionRuntime(environment)
    start = environment.current_tick
    selected = {0: choose(environment, 0, 1), 1: choose(environment, 1, 1 if same_tick else 2)}
    runtime.commit_simultaneous(selected, legal=lambda owner, action: action == selected[owner])
    runtime.advance()
    assert not runtime.freeze_due_information()
    outcomes = runtime.advance()
    frozen = runtime.freeze_due_information()
    assert environment.current_tick == start + 2
    assert set(frozen) == {0, 1} and not runtime.clock.pending
    expected = ["executed", "executed"] if same_tick else ["executed", "preempted"]
    assert [event["reason"] for event in runtime.clock.evidence] == expected
    assert all(value == "executed" for value in outcomes.values())
    with runtime.snapshot(sidecar_factory) as snapshot:
        runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
        runtime.advance()
        snapshot.restore()
        assert set(runtime.freeze_due_information()) == {0, 1}
        assert runtime.clock.tick == start + 2
        assert [event["reason"] for event in runtime.clock.evidence] == expected
    evidence = dict(case="simultaneous_due" if same_tick else "early_preemption", start_tick=start,
        settled_tick=environment.current_tick, lifecycle=runtime.clock.evidence,
        native_outcomes=[event.to_dict() for event in environment.action_outcomes()], snapshot_restore=True)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    runtime.advance()
    runtime.advance()
    assert not environment.action_outcomes(), "preempted reservation executed later"
    return evidence


def run_affordability_case(native, sidecar_factory):
    environment = FinalBattleEnv(native=native)
    environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
        seed=78101903, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
        options={"render_mode": "headless", "decision_ticks": 1,
            "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
    native.start_atomic_steps()
    for attempt in range(8):
        raw = environment.raw_observation
        player = next(player for player in raw["players"] if player["owner"] == 0)
        candidates = sorted(player["hand"], key=lambda card: int(card["cost"]), reverse=True)
        unaffordable = next((card for card in candidates if int(card["cost"]) * 10000 > int(player["elixirRaw"])), None)
        if unaffordable is not None:
            break
        action = choose(environment, 0, 1)
        outcomes, _ = environment.advance_reservations((Reservation(attempt, 0, action,
            environment.current_tick - 1, environment.current_tick + 1),))
        assert outcomes[attempt] == "executed"
    else:
        raise ValueError("probe did not obtain a future-affordable hand")
    card_id, cost, slot = int(unaffordable["cardId"]), int(unaffordable["cost"]), int(unaffordable["handIndex"])
    evidence = None
    for step in range(400):
        raw = environment.raw_observation
        player = next(player for player in raw["players"] if player["owner"] == 0)
        remaining = environment.mode_timeline.timeline.ticks_for_raw(int(raw["tick"]), cost * 10000 - int(player["elixirRaw"]))
        if remaining <= 2:
            entry = environment.action_mask(0).placement_masks[str(slot)]
            cell = next(row * 18 + column for row, cells in enumerate(entry["row_major"])
                for column, valid in enumerate(cells) if valid)
            runtime = FinalDecisionRuntime(environment)
            start = environment.current_tick
            payable = native_payment_mask(tick=int(raw["tick"]), elixir_raw=int(player["elixirRaw"]),
                cost_raw=cost * 10000, timeline=environment.mode_timeline.timeline)
            assert not bool(payable[1]), "probe must expose the one-tick payment boundary"
            with runtime.snapshot(sidecar_factory) as snapshot:
                runtime.commit_simultaneous({0: FullAction(card_id, cell, 1), 1: FullAction()}, legal=lambda *args: True)
                runtime.advance()
                rejected = runtime.advance()
                assert rejected == {0: "rejected"}
                assert set(runtime.freeze_due_information()) == {0}
                snapshot.restore()
            action = FullAction(card_id, cell, 2)
            assert bool(payable[2])
            runtime.commit_simultaneous({0: action, 1: FullAction()}, legal=lambda *args: True)
            runtime.advance()
            before = next(player for player in environment.raw_observation["players"] if player["owner"] == 0)
            before_elixir = int(before["elixirRaw"])
            runtime.advance()
            runtime.advance()
            outcomes = runtime.advance()
            evidence = dict(case="future_affordable", start_tick=start, due_tick=start + 4,
                tick_after_decision_elixir_raw=before_elixir, cost_raw=cost * 10000,
                excluded_delay_ms=100, selected_delay_ms=200, rejected_trial=rejected,
                rejection_wake_owners=[0], snapshot_restore=True, outcomes=outcomes, lifecycle=runtime.clock.evidence)
            assert before_elixir < cost * 10000, evidence
            assert outcomes == {0: "executed"}, evidence
            break
        environment.advance_reservations(())
    if evidence is None:
        raise ValueError("future-affordability threshold not reached")
    return evidence


def run_source_case(native, source, sidecar_factory):
    environment = FinalBattleEnv(native=native)
    environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
        seed=78101904, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
        options={"render_mode": "headless", "decision_ticks": 1,
            "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
    native.start_atomic_steps()
    rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101944)
    decisions = []
    with torch.inference_mode():
        for step in range(240):
            frozen = rollout.freeze_due_information()
            if frozen:
                selected = {}
                for owner, info in frozen.items():
                    policy, wdl = source_policy(source, info.frame, info.legality)
                    action, logp = policy.sample(rollout.generators[owner])
                    assert info.legality.legal(action) and torch.isfinite(wdl).all()
                    selected[owner] = action
                    decisions.append(dict(owner=owner, tick=info.tick, action=action.key,
                        fair_sha=info.sha256, logp=float(logp), wdl=wdl.softmax(-1)[0].tolist()))
                rollout.commit_simultaneous(selected)
            if len(decisions) >= 16:
                break
            outcomes = rollout.advance()
            assert all(reason == "executed" for reason in outcomes.values()), outcomes
    assert len(decisions) >= 16
    for step in range(60):
        if rollout.runtime.frozen:
            break
        outcomes = rollout.advance()
        assert all(reason == "executed" for reason in outcomes.values()), outcomes
    assert rollout.runtime.frozen
    rollout.freeze_due_information()
    with rollout.snapshot(sidecar_factory) as snapshot:
        branches = []
        for attempt in range(2):
            trace = []
            with torch.inference_mode():
                for step in range(120):
                    frozen = rollout.freeze_due_information()
                    if frozen:
                        selected = {}
                        for owner, info in frozen.items():
                            policy, wdl = source_policy(source, info.frame, info.legality)
                            action, logp = policy.sample(rollout.generators[owner])
                            selected[owner] = action
                            trace.append(dict(owner=owner, tick=info.tick, fair_sha=info.sha256,
                                action=action.key, logp=float(logp), value=wdl.tolist(),
                                frame_fields={field.name: semantic_digest(getattr(info.frame, field.name))
                                    for field in fields(info.frame)},
                                legality_sha=semantic_digest(info.legality.plays)))
                        rollout.commit_simultaneous(selected)
                    outcomes = rollout.advance()
                    assert all(reason == "executed" for reason in outcomes.values()), outcomes
                    if len(trace) >= 6 and rollout.runtime.frozen:
                        break
            assert len(trace) >= 6
            branches.append(dict(trace=trace, digest=native._request("state-digest")["sha256"],
                clock=rollout.runtime.clock.capture()))
            snapshot.restore()
        max_numeric_error = 0.
        for left, right in zip(branches[0]["trace"], branches[1]["trace"]):
            for key in ("logp", "value"):
                before, after = torch.tensor(left[key]), torch.tensor(right[key])
                max_numeric_error = max(max_numeric_error, float((before - after).abs().max()))
                torch.testing.assert_close(before, after, atol=1e-6, rtol=1e-6)
        comparable = [{**branch, "trace": [{key: value for key, value in entry.items()
            if key not in ("logp", "value")} for entry in branch["trace"]]} for branch in branches]
        if comparable[0] != comparable[1]:
            difference = dict(digest_equal=branches[0]["digest"] == branches[1]["digest"],
                clock_equal=branches[0]["clock"] == branches[1]["clock"],
                lengths=[len(branch["trace"]) for branch in branches])
            difference["first_difference"] = next(((left, right) for left, right
                in zip(branches[0]["trace"], branches[1]["trace"]) if left != right), None)
            raise AssertionError(json.dumps(difference))
        frozen = rollout.freeze_due_information()
        if frozen:
            rollout.commit_simultaneous({owner: FullAction() for owner in frozen})
        assert rollout.runtime.clock.pending and not rollout.runtime.clock.ready
        with rollout.snapshot(sidecar_factory) as pending_snapshot:
            pending_results = []
            for attempt in range(2):
                rollout.runtime.failed = True
                pending_snapshot.restore()
                for step in range(60):
                    if rollout.runtime.frozen:
                        break
                    rollout.advance()
                assert rollout.runtime.frozen
                pending_results.append(dict(
                    frames={owner: info.sha256 for owner, info in rollout.freeze_due_information().items()},
                    clock=rollout.runtime.clock.capture(), digest=native._request("state-digest")["sha256"]))
            assert pending_results[0] == pending_results[1], "pending rollout recovery diverged"
        snapshot.restore()
    return dict(case="source_a_fair_decisions", decisions=decisions, end_tick=environment.current_tick,
        complete_episode=False, training_admissible=False, full_rollout_restore=True,
        restored_branch_decisions=len(branches[0]["trace"]), fp32_max_absolute_error=max_numeric_error,
        pending_snapshot_restore=True, failed_runtime_recovery=True)


def run_hidden_draw_case(native, source, sidecar_factory):
    from native_runner.training.fair_search.native import NativeBeliefWorld

    environment = FinalBattleEnv(native=native)
    environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
        seed=78101905, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
        options={"render_mode": "headless", "decision_ticks": 1,
            "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
    native.start_atomic_steps()
    rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101945)
    evidence = []
    with rollout.snapshot(sidecar_factory) as snapshot, torch.inference_mode():
        for owner in (0, 1):
            snapshot.restore()
            before = rollout.freeze_due_information()[owner]
            policy, value = source_policy(source, before.frame, before.legality)
            candidates = structured_candidates(policy, seed=78101955)
            action, logp = policy.sample(torch.Generator().manual_seed(78101965))
            original = native.observe()
            digest_before = native._request("state-digest")["sha256"]
            expected, replacement = [], []
            for current_owner in (0, 1):
                player = next(player for player in original["players"] if player["owner"] == current_owner)
                queue = [-1] * 4
                for card in player["hand"]:
                    queue[card["handIndex"]] = card["deckSlot"]
                queue.extend(card["deckSlot"] for card in sorted(player["cycle"], key=lambda card: card["cycleIndex"]))
                expected.extend([*queue, *([-2] * (12 - len(queue)))])
                if current_owner != owner:
                    queue[0], queue[-1] = queue[-1], queue[0]
                replacement.extend([*queue, *([-2] * (12 - len(queue)))])
            payload = [original["generation"], original["stateEpoch"], original["tick"], *expected, *replacement]
            raw_receipt = native._request("belief-draw-probe " + " ".join(map(str, payload)))
            assert raw_receipt.get("ok") is True
            digest_after = native._request("state-digest")["sha256"]
            assert digest_after != digest_before, "hidden draw perturbation did not change native state"
            environment._raw = dict(native.observe())
            environment._invalidate_rich_cache()
            receipt = SimpleNamespace(generation=int(raw_receipt["generation"]),
                state_epoch=int(raw_receipt["stateEpoch"]), tick=int(raw_receipt["tick"]))
            NativeBeliefWorld._rebase_native_cursors(SimpleNamespace(runtime=rollout.runtime),
                environment._rich_snapshot_for(environment.raw_observation), receipt)
            changed = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101945)
            after = changed.freeze_due_information()[owner]
            changed_policy, changed_value = source_policy(source, after.frame, after.legality)
            changed_candidates = structured_candidates(changed_policy, seed=78101955)
            changed_action, changed_logp = changed_policy.sample(torch.Generator().manual_seed(78101965))
            assert before.sha256 == after.sha256
            assert candidates == changed_candidates
            assert action == changed_action and torch.equal(logp, changed_logp)
            assert torch.equal(value, changed_value)
            assert changed.tensorizers[owner].tracker.decks[1 - owner] == ()
            evidence.append(dict(owner=owner, native_hidden_changed=True, fair_sha=before.sha256,
                candidate_count=len(candidates), sampled_action=action.key, exact_cpu_outputs=True))
        snapshot.restore()
    return dict(case="native_opponent_draw_noninterference", seats=evidence,
        scope="opening true opponent hand/cycle perturbation; not a complete search FAIR gate")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    torch.set_num_threads(1)
    started = time.monotonic()
    sys.path.insert(0, str(args.puct_root.resolve()))
    from native_adapter import BattleEnvSidecar
    results = []
    for index, same_tick in enumerate((True, False)):
        native = create_search_native(args.port, transport="compact-delta")
        try:
            results.append(run_case(native, seed=78101901 + index, same_tick=same_tick, sidecar_factory=BattleEnvSidecar))
        finally:
            native.close_transport()
    native = create_search_native(args.port, transport="compact-delta")
    try:
        results.append(run_affordability_case(native, BattleEnvSidecar))
    finally:
        native.close_transport()
    if args.source is not None:
        from native_runner.training.fair_search.model import load_checkpoint

        source, _, identity = load_checkpoint(args.source, device="cuda:0",
            expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
        source.eval()
        native = create_search_native(args.port, transport="compact-delta")
        try:
            results.append({**run_source_case(native, source, BattleEnvSidecar), "source_sha256": identity})
        finally:
            native.close_transport()
        source.to("cpu")
        native = create_search_native(args.port, transport="compact-delta")
        try:
            results.append(run_hidden_draw_case(native, source, BattleEnvSidecar))
        finally:
            native.close_transport()
    report = dict(schema="fair-final-native-timing-probe.v1", status="passed", cases=results,
        seconds=time.monotonic() - started)
    manifest = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    if manifest.is_file():
        report["source_sha256"] = json.loads(manifest.read_text())
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(status=report["status"], cases=[case["case"] for case in results],
        seconds=report["seconds"], report=str(args.output))))


if __name__ == "__main__":
    main()
