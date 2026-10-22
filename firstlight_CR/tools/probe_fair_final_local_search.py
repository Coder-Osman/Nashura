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
from native_runner.training.fair_final.local_reconditioning import SettledLocalSearchProvider
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.prior_history import PriorHistoryBeliefFactory
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.probe_fair_final_prior_search import PriorAuditProvider, SOURCE_SHA
from tools.probe_fair_final_visibility import perturb_draw, refreshed_information


ANCHOR_SEED = 78102301
SEARCH_SEED = 78102103
BEHAVIOR_SEED = 78102104
EXCLUSIONS = ["population-prior-calibration", "single-ready-pending-plan", "incomplete-refills",
    "overtime-root", "multi-slot-telemetry", "full-episode-search-quality",
    "training-admission", "end-to-end-throughput"]


def phases(rollout):
    environment = rollout.runtime.environment
    return {str(item.native_object_id): asdict(item.phase_runtime)
        for item in environment._rich_snapshot_for(environment.raw_observation).objects
        if item is not None and item.phase_runtime is not None}


class LocalAuditProvider(PriorAuditProvider):
    def build(self, information, *, seed):
        world = super().build(information, seed=seed)
        self.local_profile = deepcopy(world.local_profile)
        self.anchor_profile = deepcopy(self.provider.cursor.session.profile)
        self.anchor_snapshot = self.provider.cursor.session.root
        self.anchor_retained = False
        root_phases = phases(world.rollout)
        self.lifecycle["phase_object_count"] = len(root_phases)
        self.lifecycle["phase_restores"] = 0
        restore = world.restore_root
        close = world.close

        def restore_verified():
            restore()
            if phases(world.rollout) != root_phases:
                raise ValueError("local search restore changed native phase state")
            self.lifecycle["phase_restores"] += 1

        def close_verified():
            try:
                if not world.closed:
                    restore_verified()
            finally:
                close()
            self.anchor_retained = self.anchor_snapshot.native.handle is not None
            if self.provider.active is not None or not self.anchor_retained:
                raise ValueError("search release lost anchor or leaked lease")
            self.anchor_restored_sha = world.rollout.freeze_due_information()[information.owner].sha256
            if self.anchor_restored_sha != information.sha256:
                raise ValueError("search release changed anchor FAIR state")

        world.restore_root = restore_verified
        world.close = close_verified
        return world


def main():
    parser = argparse.ArgumentParser()
    for name in ("port", "belief-port"):
        parser.add_argument("--"+name, type=int, required=True)
    for name in ("source", "puct-root", "prior", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--threshold", type=int, choices=(1200, 3600), required=True)
    parser.add_argument("--owner", type=int, choices=(0, 1), required=True)
    args = parser.parse_args()
    if args.port == args.belief_port:
        raise ValueError("independent actual and belief engines required")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic()-started >= 3540:
            raise TimeoutError("bounded local Full128 search deadline")

    project = Path(__file__).resolve().parents[1]
    manifest = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("local Full128 deployed source changed")
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    limits = SearchLimits()
    native = create_search_native(args.port, transport="compact-delta")
    factory = PriorHistoryBeliefFactory(lambda: create_search_native(args.belief_port, transport="compact-delta"),
        prior=prior, catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    records = []
    provider = None
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101971)
        initial = rollout.freeze_due_information()
        recorders = {owner: FairHistoryRecorder(rollout.runtime.frozen[owner], info,
            opening_tick=environment.warmup_ticks) for owner, info in initial.items()}

        def advance_to(threshold):
            while environment.current_tick < threshold or rollout.runtime.clock.ready != {0, 1}:
                deadline()
                if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                    raise ValueError("actual trajectory ended before registered coverage")
                frozen = rollout.freeze_due_information()
                if frozen:
                    actions = {owner: inference.sample(info, rollout.generators[owner]) for owner, info in frozen.items()}
                    rollout.commit_simultaneous(actions)
                    for owner, info in frozen.items():
                        recorders[owner].record_decision(info, actions[owner])
                if any(reason != "executed" for reason in rollout.advance().values()):
                    raise ValueError("actual trajectory rejected reservation")
                observations = environment.observe_dual(policy_features_only=True)
                for owner in (0, 1):
                    recorders[owner].observe_tick(observations[owner])

        advance_to(args.threshold)
        first_tick = environment.current_tick
        for stage in ("initial", "successor"):
            if stage == "successor":
                advance_to(first_tick+1)
            print(json.dumps(dict(phase="stage-ready", stage=stage, owner=args.owner,
                tick=environment.current_tick, seconds=time.monotonic()-started)), flush=True)
            actual_digest = native._request("state-digest")["sha256"]
            actual_phases = phases(rollout)
            with rollout.snapshot(BattleEnvSidecar) as actual_root:
                for altered in (False, True):
                    actual_root.restore()
                    if phases(rollout) != actual_phases:
                        raise ValueError("actual native phases changed during root restore")
                    mutation = perturb_draw(rollout, args.owner) if altered else None
                    information = refreshed_information(rollout)[args.owner]
                    history = recorders[args.owner].capture(information)
                    atomic_json(args.output.with_name(f"history-{stage}-{int(altered)}.json"), history.to_payload())
                    if provider is None:
                        provider = SettledLocalSearchProvider(factory, history=history,
                            anchor_seed=ANCHOR_SEED, check_deadline=deadline)
                    else:
                        provider.history = history
                    audited = LocalAuditProvider(provider)
                    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                        checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
                    before = native._request("state-digest")["sha256"]
                    print(json.dumps(dict(phase="search-started", stage=stage, altered=altered,
                        owner=args.owner, tick=information.tick)), flush=True)
                    outcome = CompletedBoundarySearch(audited, inference, inference, limits=limits).run(
                        information, seed=SEARCH_SEED, behavior_seed=BEHAVIOR_SEED, codec=ActionCodec(HOG_DECK),
                        identity=identity, deadline=started+3540)
                    after = native._request("state-digest")["sha256"]
                    if before != after or provider.active is not None:
                        raise ValueError("local Full128 changed actual world or leaked lease")
                    records.append(dict(stage=stage, owner=args.owner, threshold=args.threshold, altered=altered,
                        tick=information.tick, fair_sha=information.sha256, history_sha=history.sha256,
                        outcome=dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target),
                        audit=outcome.audit, attempts=audited.attempts, world_lifecycle=audited.lifecycle,
                        local_profile=audited.local_profile, anchor_profile=audited.anchor_profile,
                        anchor_retained=audited.anchor_retained, anchor_restored_sha=audited.anchor_restored_sha,
                        mutation=mutation, actual_digest_before=before, actual_digest_after=after, lease_released=True))
                    atomic_json(args.output.with_name("progress.json"), dict(records=records))
                    print(json.dumps(dict(phase="search-completed", stage=stage, altered=altered,
                        simulations=outcome.result["valid_simulations"], seconds=outcome.audit["seconds"],
                        local_profile=audited.local_profile, timings=outcome.audit["timings"])), flush=True)
                for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle"):
                    if records[-2][key] != records[-1][key]:
                        raise ValueError("hidden state changed local Full128 "+key)
                actual_root.restore()
            if (native._request("state-digest")["sha256"] != actual_digest
                    or actual_root.native.handle is not None or phases(rollout) != actual_phases):
                raise ValueError("actual root restoration or snapshot release failed")
        anchor = provider.cursor.session.root
        cursor_events = deepcopy(provider.cursor.events)
        provider.close()
        if (anchor.native.handle is not None or not provider.closed or provider.cursor.session is not None
                or provider.active is not None):
            raise ValueError("persistent provider cleanup failed")
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen model changed")
        atomic_json(args.output, dict(schema="fair-final-native-local-search.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            prior_sha=prior.sha256, prior_file_sha=hashlib.sha256(args.prior.read_bytes()).hexdigest(),
            threshold=args.threshold, owner=args.owner, limits=asdict(limits), records=records,
            cursor_events=cursor_events, provider_closed=True, anchor_snapshot_released=True,
            model_unchanged=True, actual_root_restored=True, snapshot_released=True,
            seconds=time.monotonic()-started, exclusions=EXCLUSIONS))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, seconds=time.monotonic()-started, source_sha256=manifest))
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
