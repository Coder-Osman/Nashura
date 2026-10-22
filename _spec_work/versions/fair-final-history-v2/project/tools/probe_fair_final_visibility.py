import argparse
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.contracts import EventV1
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.event_cache import canonicalize_event_observation
from native_runner.training.fair_final.adapter import legal_prefixes, source_policy
from native_runner.training.fair_final.candidates import structured_candidates
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import DecisionInformation, FinalRollout
from native_runner.training.fair_final.visibility import event_visible_to_owner, validate_fair_view
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.native import NativeBeliefWorld
from native_runner.training.fair_search.transport import create_search_native


def policy_signature(source, info):
    policy, value = source_policy(source, info.frame, info.legality)
    candidates = structured_candidates(policy, seed=78101985)
    action, logp = policy.sample(torch.Generator().manual_seed(78101995))
    prefixes = [()]
    if bool(info.legality.cards.any()):
        prefixes.append((1,))
    for candidate in candidates:
        if candidate.action.card_id is not None:
            card = info.legality.card_ids.index(candidate.action.card_id)
            prefixes.extend(((1, card), (1, card, candidate.action.cell)))
    conditional = [(prefix, policy.conditionals(prefix).detach().cpu()) for prefix in sorted(set(prefixes))]
    return dict(fair_sha=info.sha256, conditionals_sha=semantic_digest(conditional),
        candidates_sha=semantic_digest([asdict(candidate) for candidate in candidates]),
        action=action.key, logp=float(logp), wdl=value.tolist())


def refreshed_information(rollout):
    observations = rollout.runtime.environment.observe_dual(policy_features_only=True)
    rollout.runtime.frozen = {owner: observations[owner] for owner in rollout.runtime.clock.ready}
    rollout.information = {}
    return rollout.freeze_due_information()


def perturb_draw(rollout, owner):
    environment = rollout.runtime.environment
    native = environment.native
    original = native.observe()
    before = native._request("state-digest")["sha256"]
    expected, replacement = [], []
    for current_owner in (0, 1):
        player = next(player for player in original["players"] if player["owner"] == current_owner)
        queue = [-1] * 4
        for card in player["hand"]:
            queue[card["handIndex"]] = card["deckSlot"]
        queue.extend(card["deckSlot"] for card in sorted(player["cycle"], key=lambda card: card["cycleIndex"]))
        expected.extend([*queue, *([-2] * (12 - len(queue)))])
        if current_owner != owner:
            hand_index = next(index for index in range(4) if queue[index] >= 0)
            queue[hand_index], queue[-1] = queue[-1], queue[hand_index]
        replacement.extend([*queue, *([-2] * (12 - len(queue)))])
    payload = [original["generation"], original["stateEpoch"], original["tick"], *expected, *replacement]
    receipt = native._request("belief-draw-probe " + " ".join(map(str, payload)))
    if receipt.get("ok") is not True or native._request("state-digest")["sha256"] == before:
        raise ValueError("native hidden draw mutation was not applied")
    environment._raw = dict(native.observe())
    environment._invalidate_rich_cache()
    native_receipt = SimpleNamespace(generation=int(receipt["generation"]),
        state_epoch=int(receipt["stateEpoch"]), tick=int(receipt["tick"]))
    NativeBeliefWorld._rebase_native_cursors(SimpleNamespace(runtime=rollout.runtime),
        environment._rich_snapshot_for(environment.raw_observation), native_receipt)
    return dict(before_digest=before, after_digest=native._request("state-digest")["sha256"])


def check_boundary(rollout, source, sidecar_factory, stage):
    environment = rollout.runtime.environment
    config = environment.match_config
    records = []
    with rollout.snapshot(sidecar_factory) as snapshot:
        try:
            for owner in (0, 1):
                snapshot.restore()
                before_info = refreshed_information(rollout)[owner]
                before = policy_signature(source, before_info)
                before_view = semantic_digest(rollout.runtime.frozen[owner])
                revealed = len(rollout.tensorizers[owner].tracker.decks[1 - owner])
                snapshot.restore()
                mutation = perturb_draw(rollout, owner)
                after_info = refreshed_information(rollout)[owner]
                if before_view != semantic_digest(rollout.runtime.frozen[owner]):
                    raise ValueError("native hidden draw changed the actor FAIR observation")
                if before != policy_signature(source, after_info):
                    raise ValueError("native hidden draw changed policy, candidates or value")
                records.append(dict(stage=stage, tick=before_info.tick, owner=owner, kind="native_hidden_draw",
                    exact=True, opponent_revealed= revealed, evidence=mutation))
                snapshot.restore()
                environment.match_config = replace(config, seed=config.seed + 101,
                    **{f"deck{1 - owner}": tuple(reversed(getattr(config, f"deck{1 - owner}")))})
                after_info = refreshed_information(rollout)[owner]
                if before != policy_signature(source, after_info):
                    raise ValueError("private reset metadata changed actor output")
                records.append(dict(stage=stage, tick=before_info.tick, owner=owner,
                    kind="reset_seed_and_hidden_deck_metadata", exact=True))
                environment.match_config = config
                snapshot.restore()
                tick = environment.current_tick
                injected = (
                    EventV1(tick=tick, event_type="action_executed", owner=1-owner, card_id=HOG_DECK[0],
                        data={"private_to": 1-owner, "probe": True}),
                    EventV1(tick=tick, event_type="action_executed", owner=1-owner, card_id=HOG_DECK[1],
                        data={"oracle_only": True, "probe": True}))
                for event in injected:
                    environment._append_event(event)
                after_info = refreshed_information(rollout)[owner]
                if before_view != semantic_digest(rollout.runtime.frozen[owner]) or before != policy_signature(source, after_info):
                    raise ValueError("private event injection changed actor output")
                records.append(dict(stage=stage, tick=tick, owner=owner, kind="private_events", exact=True))
                plan_results = []
                for with_plan in (False, True):
                    snapshot.restore()
                    frozen = refreshed_information(rollout)
                    enemy = 1 - owner
                    plays = torch.nonzero(frozen[enemy].legality.plays, as_tuple=False)
                    if not len(plays):
                        raise ValueError("hidden-plan test requires at least one legal ACT")
                    card, cell, delay = (int(value) for value in plays[-1])
                    action = FullAction(frozen[enemy].legality.card_ids[card], cell, delay)
                    rollout.commit_simultaneous({owner: FullAction(), enemy: action if with_plan else FullAction()})
                    rollout.advance()
                    observation = environment.observe_dual(policy_features_only=True)[owner]
                    validate_fair_view(observation, owner)
                    frame = canonicalize_event_observation(rollout.tensorizers[owner].tensorize(observation, validate=True))
                    plan_results.append(dict(view=semantic_digest(observation), frame=semantic_digest(frame),
                        wake=owner in rollout.runtime.clock.ready,
                        digest=environment.native._request("state-digest")["sha256"]))
                if plan_results[0] != plan_results[1] or plan_results[0]["wake"]:
                    raise ValueError("unexecuted enemy plan changed own next-tick view or wake")
                records.append(dict(stage=stage, tick=tick, owner=owner, kind="pending_enemy_plan",
                    exact=True, scope="one tick before earliest legal execution; diagnostic frame only"))
        finally:
            environment.match_config = config
            snapshot.restore()
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar
    started = time.monotonic()
    source, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("deployed visibility source identity differs")
    native = create_search_native(args.port, transport="compact-delta")
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK), seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options={"render_mode": "headless", "decision_ticks": 1, "event_driven_decisions": False,
                "allow_initial_remaining_runtime_rejections": True})
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101971)
        stages = [("opening", 100), ("middle", 1200), ("late", 3600), ("overtime", 5400)]
        checks, decisions, counts = [], [], Counter()
        with torch.inference_mode():
            while not rollout.runtime.ended:
                if time.monotonic() - started > 1200 or rollout.runtime.failed or rollout.runtime.truncated:
                    raise ValueError("visibility probe exceeded bound or produced invalid episode")
                if (stages and rollout.runtime.clock.ready == {0, 1}
                        and environment.current_tick >= stages[0][1]):
                    stage, _ = stages.pop(0)
                    checks.extend(check_boundary(rollout, source, BattleEnvSidecar, stage))
                    print(json.dumps(dict(stage=stage, tick=environment.current_tick, checks=len(checks))), flush=True)
                frozen = rollout.freeze_due_information()
                if frozen:
                    selected = {}
                    for owner, info in frozen.items():
                        observation = rollout.runtime.frozen[owner]
                        validate_fair_view(observation, owner)
                        if any(not event_visible_to_owner(event, owner) for event in observation.events):
                            raise ValueError("nonpublic event escaped native observation filtering")
                        counts[str(owner)] += 1
                        policy, value = source_policy(source, info.frame, info.legality)
                        selected[owner], _ = policy.sample(rollout.generators[owner])
                        if not info.legality.legal(selected[owner]) or not torch.isfinite(value).all():
                            raise ValueError("invalid policy output")
                        decisions.append(dict(owner=owner, tick=info.tick, fair_sha=info.sha256,
                            action=selected[owner].key, visible_events=len(observation.events)))
                    rollout.commit_simultaneous(selected)
                outcomes = rollout.advance()
                if any(reason != "executed" for reason in outcomes.values()):
                    raise ValueError("native execution rejection")
        if stages or len(checks) != 32 or min(counts.values()) < 1:
            raise ValueError("trajectory did not cover all registered stages and seats")
        terminal = terminal_result(environment.observe_dual(policy_features_only=True)[0].terminal,
            environment.raw_observation, truncated=rollout.runtime.truncated, failed=rollout.runtime.failed)
        report = dict(schema="fair-final-trajectory-visibility.v1", status="passed", training_admissible=False,
            source_sha256=manifest, checkpoint_sha256=source_sha, checks=checks, decisions=decisions,
            decisions_by_owner=dict(counts), terminal=terminal, seconds=time.monotonic()-started,
            exclusions=["native PRNG state mutation", "compatible hidden posterior proof",
                "Full128 search and simulated-opponent end-to-end isolation", "all hidden entity mechanics"])
        atomic_json(args.output, report)
        print(json.dumps({key: report[key] for key in ("status", "seconds", "terminal", "decisions_by_owner")}), flush=True)
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
