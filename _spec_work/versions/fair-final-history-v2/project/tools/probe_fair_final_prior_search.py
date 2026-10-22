import argparse
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
from native_runner.training.fair_final.prior_history import PriorHistoryBeliefFactory
from native_runner.training.fair_final.prior_provider import PriorHistorySearchProvider
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.probe_fair_final_extended_search import BoundaryAuditProvider, SOURCE_SHA
from tools.probe_fair_final_visibility import perturb_draw, refreshed_information


class PriorAuditProvider(BoundaryAuditProvider):
    def __init__(self, provider):
        super().__init__(provider)
        self.lifecycle = dict(builds=0, restores=0, closed=False, snapshot_released=False)

    def build(self, information, *, seed):
        world = super().build(information, seed=seed)
        self.lifecycle["builds"] += 1
        native = world.rollout.runtime.environment.native
        try:
            self.lifecycle["root_digest"] = native._request("state-digest")["sha256"]
        except BaseException:
            world.close()
            raise
        restore, close = world.restore_root, world.close

        def restore_verified():
            restore()
            if native._request("state-digest")["sha256"] != self.lifecycle["root_digest"]:
                raise ValueError("prior search restored native root changed")
            self.lifecycle["restores"] += 1

        def close_verified():
            try:
                if not world.closed:
                    restore_verified()
            finally:
                close()
                self.lifecycle["closed"] = world.closed
                self.lifecycle["snapshot_released"] = world.root.native.handle is None

        world.restore_root, world.close = restore_verified, close_verified
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
            raise TimeoutError("bounded prior search deadline")

    manifest_path = Path(__file__).resolve().parents[1]/"probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("prior search deployed source changed")
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
        while environment.current_tick < args.threshold or rollout.runtime.clock.ready != {0, 1}:
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
        print(json.dumps(dict(phase="stage-ready", threshold=args.threshold, owner=args.owner,
            tick=environment.current_tick, seconds=time.monotonic()-started)), flush=True)
        actual_digest = native._request("state-digest")["sha256"]
        with rollout.snapshot(BattleEnvSidecar) as actual_root:
            for altered in (False, True):
                actual_root.restore()
                mutation = perturb_draw(rollout, args.owner) if altered else None
                information = refreshed_information(rollout)[args.owner]
                history = recorders[args.owner].capture(information)
                atomic_json(args.output.with_name(f"history-{int(altered)}.json"), history.to_payload())
                provider = PriorHistorySearchProvider(factory, history=history, check_deadline=deadline)
                audited = PriorAuditProvider(provider)
                identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                    checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                    controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
                before = native._request("state-digest")["sha256"]
                print(json.dumps(dict(phase="search-started", owner=args.owner, altered=altered,
                    threshold=args.threshold)), flush=True)
                try:
                    outcome = CompletedBoundarySearch(audited, inference, inference, limits=limits).run(
                        information, seed=78102103, behavior_seed=78102104, codec=ActionCodec(HOG_DECK),
                        identity=identity, deadline=started+3540)
                    if provider.active is not None:
                        raise ValueError("prior search world lease leaked")
                finally:
                    provider.close()
                after = native._request("state-digest")["sha256"]
                if before != after:
                    raise ValueError("prior search changed actual world")
                records.append(dict(owner=args.owner, threshold=args.threshold, altered=altered,
                    tick=information.tick, fair_sha=information.sha256, history_sha=history.sha256,
                    outcome=dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target),
                    audit=outcome.audit, attempts=audited.attempts, world_lifecycle=audited.lifecycle,
                    mutation=mutation, actual_digest_before=before, actual_digest_after=after, lease_released=True))
                atomic_json(args.output.with_name("progress.json"), dict(records=records))
                print(json.dumps(dict(phase="search-completed", owner=args.owner, altered=altered,
                    simulations=outcome.result["valid_simulations"], seconds=outcome.audit["seconds"],
                    timings=outcome.audit["timings"])), flush=True)
            for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle"):
                if records[0][key] != records[1][key]:
                    raise ValueError("hidden state changed prior search "+key)
            actual_root.restore()
        if native._request("state-digest")["sha256"] != actual_digest or actual_root.native.handle is not None:
            raise ValueError("actual root restoration or snapshot release failed")
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen model changed")
        atomic_json(args.output, dict(schema="fair-final-native-prior-search.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            prior_sha=prior.sha256, prior_file_sha=hashlib.sha256(args.prior.read_bytes()).hexdigest(),
            threshold=args.threshold, owner=args.owner, limits=asdict(limits), records=records,
            model_unchanged=True, actual_root_restored=True, snapshot_released=True,
            seconds=time.monotonic()-started, exclusions=["population-prior-calibration",
                "native-local-reconditioning", "normal-single-ready-root", "overtime-root",
                "full-episode-search-quality", "training-admission", "end-to-end-throughput"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, seconds=time.monotonic()-started, source_sha256=manifest))
        raise
    finally:
        factory.close()
        native.close_transport()


if __name__ == "__main__":
    main()
