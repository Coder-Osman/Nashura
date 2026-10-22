import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.refill_belief import native_draw_layout
from native_runner.training.fair_final.refill_local import IncrementalRefillBeliefFactory, RefillLocalSearchProvider
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.probe_fair_final_local_search import LocalAuditProvider, phases
from tools.probe_fair_final_prior_search import SOURCE_SHA
from tools.probe_fair_final_visibility import perturb_draw, refreshed_information


ANCHOR_SEED = 78102401
SEARCH_SEED = 78102603
BEHAVIOR_SEED = 78102604
EXCLUSIONS = ["full-posterior-resampling", "general-pending-plan", "complete-game-search",
    "population-prior-calibration", "throughput-ab", "training-admission"]


class RefillProviderAudit(LocalAuditProvider):
    def build(self, information, *, seed):
        world = super().build(information, seed=seed)
        try:
            session = self.provider.cursor.session
            self.anchor_completion = asdict(session.draw_completion)
            with world.rollout.snapshot(self.provider.factory.sidecar_factory) as local:
                session.root.restore()
                self.anchor_layouts = [asdict(native_draw_layout(player))
                    for player in world.rollout.runtime.environment.native.observe()["players"]]
                local.restore()
            self.local_layouts = [asdict(native_draw_layout(player))
                for player in world.rollout.runtime.environment.native.observe()["players"]]
        except BaseException:
            world.close()
            raise
        return world


def main():
    parser = argparse.ArgumentParser()
    for name in ("port", "belief-port", "tick", "owner"):
        parser.add_argument("--"+name, type=int, required=True)
    for name in ("source", "puct-root", "prior", "trace", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    if args.port == args.belief_port or args.owner not in (0, 1) or args.tick not in (110, 2598, 5602):
        raise ValueError("refill provider requires registered roots and independent engines")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic()-started >= 5400:
            raise TimeoutError("bounded refill provider Full128 deadline")

    project = Path(__file__).resolve().parents[1]
    manifest = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("refill provider deployed source changed")
    trace = json.loads(args.trace.read_text())
    groups = {group["tick"]: group for group in trace["boundaries"]}
    successor = next(group["tick"] for group in trace["boundaries"] if group["tick"] > args.tick)
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    limits = SearchLimits()
    native = create_search_native(args.port, transport="compact-delta")
    factory = IncrementalRefillBeliefFactory(lambda: create_search_native(args.belief_port, transport="compact-delta"),
        prior=prior, catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    records, replayed = [], []
    provider = None
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101906,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101946)
        initial = rollout.freeze_due_information()
        recorders = {owner: FairHistoryRecorder(rollout.runtime.frozen[owner], info,
            opening_tick=environment.warmup_ticks) for owner, info in initial.items()}

        def advance_to(target):
            while environment.current_tick < target:
                deadline()
                if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                    raise ValueError("actual refill trajectory terminated before requested root")
                frozen = rollout.freeze_due_information()
                if frozen:
                    tick = environment.current_tick
                    group = groups[tick]
                    observed = {str(owner): info.sha256 for owner, info in frozen.items()}
                    if observed != group["fair_sha"]:
                        raise ValueError("actual refill replay changed an archived FAIR boundary")
                    actions = {int(owner): FullAction(**action) for owner, action in group["actions"].items()}
                    rollout.commit_simultaneous(actions)
                    for owner, info in frozen.items():
                        recorders[owner].record_decision(info, actions[owner])
                    replayed.append(dict(tick=tick, fair_sha=observed))
                if any(reason != "executed" for reason in rollout.advance().values()):
                    raise ValueError("actual refill replay rejected an archived action")
                observations = environment.observe_dual(policy_features_only=True)
                for owner in (0, 1):
                    recorders[owner].observe_tick(observations[owner])

        for stage, tick in (("initial", args.tick), ("successor", successor)):
            advance_to(tick)
            if rollout.runtime.clock.ready != {0, 1}:
                raise ValueError("registered refill root is not a simultaneous decision boundary")
            actual_digest = native._request("state-digest")["sha256"]
            actual_phases = phases(rollout)
            with rollout.snapshot(BattleEnvSidecar) as actual_root:
                for altered in (False, True):
                    actual_root.restore()
                    mutation = perturb_draw(rollout, args.owner) if altered else None
                    information = refreshed_information(rollout)[args.owner]
                    history = recorders[args.owner].capture(information)
                    if information.sha256 != groups[tick]["fair_sha"][str(args.owner)]:
                        raise ValueError("hidden draw mutation changed root FAIR information")
                    atomic_json(args.output.with_name(f"history-{stage}-{int(altered)}.json"), history.to_payload())
                    if provider is None:
                        provider = RefillLocalSearchProvider(factory, history=history,
                            anchor_seed=ANCHOR_SEED, check_deadline=deadline)
                    else:
                        provider.history = history
                    audited = RefillProviderAudit(provider)
                    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                        checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
                    before = native._request("state-digest")["sha256"]
                    print(json.dumps(dict(phase="search-started", stage=stage, altered=altered,
                        owner=args.owner, tick=tick)), flush=True)
                    outcome = CompletedBoundarySearch(audited, inference, inference, limits=limits).run(
                        information, seed=SEARCH_SEED, behavior_seed=BEHAVIOR_SEED, codec=ActionCodec(HOG_DECK),
                        identity=identity, deadline=started+5400)
                    after = native._request("state-digest")["sha256"]
                    if before != after or provider.active is not None:
                        raise ValueError("refill provider search changed actual world or leaked lease")
                    records.append(dict(stage=stage, owner=args.owner, altered=altered, tick=tick,
                        fair_sha=information.sha256, history_sha=history.sha256,
                        outcome=dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target),
                        audit=outcome.audit, attempts=audited.attempts, world_lifecycle=audited.lifecycle,
                        local_profile=audited.local_profile, anchor_profile=audited.anchor_profile,
                        anchor_completion=audited.anchor_completion, anchor_layouts=audited.anchor_layouts,
                        local_layouts=audited.local_layouts, anchor_retained=audited.anchor_retained,
                        anchor_restored_sha=audited.anchor_restored_sha, mutation=mutation,
                        actual_digest_before=before, actual_digest_after=after, lease_released=True))
                    atomic_json(args.output.with_name("progress.json"), dict(records=records, replayed=replayed))
                    print(json.dumps(dict(phase="search-completed", stage=stage, altered=altered,
                        tick=tick, simulations=outcome.result["valid_simulations"],
                        seconds=outcome.audit["seconds"])), flush=True)
                for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle",
                        "anchor_completion", "anchor_layouts", "local_layouts"):
                    if records[-2][key] != records[-1][key]:
                        raise ValueError("true hidden state changed refill provider "+key)
                actual_root.restore()
            if (native._request("state-digest")["sha256"] != actual_digest
                    or phases(rollout) != actual_phases or actual_root.native.handle is not None):
                raise ValueError("actual refill root restoration or snapshot release failed")
        anchor = provider.cursor.session.root
        cursor_events = deepcopy(provider.cursor.events)
        provider.close()
        if (anchor.native.handle is not None or provider.cursor.session is not None
                or not provider.closed or provider.active is not None):
            raise ValueError("refill persistent provider cleanup failed")
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("refill provider mutated frozen model")
        atomic_json(args.output, dict(schema="fair-final-refill-provider-native.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            prior_sha=prior.sha256, input_trace_sha256=hashlib.sha256(args.trace.read_bytes()).hexdigest(),
            tick=args.tick, owner=args.owner, limits=asdict(limits), records=records, replayed=replayed,
            cursor_events=cursor_events, provider_closed=True, anchor_snapshot_released=True,
            model_unchanged=True, actual_root_restored=True, snapshot_released=True,
            seconds=time.monotonic()-started, exclusions=EXCLUSIONS))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, replayed=replayed, seconds=time.monotonic()-started))
        raise
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            factory.close()
            native.close_transport()


if __name__ == "__main__":
    main()
