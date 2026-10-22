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
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.history_belief import HistoryBeliefFactory
from native_runner.training.fair_final.history_provider import HistorySearchProvider
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.probe_fair_final_visibility import perturb_draw, refreshed_information


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


class BoundaryAuditProvider:
    def __init__(self, provider):
        self.provider = provider
        self.sampler_sha = provider.sampler_sha
        self.attempts = []

    def build(self, information, *, seed):
        world = self.provider.build(information, seed=seed)
        original_restore = world.restore_root
        original_transition = world.transition
        current = None

        def restore():
            nonlocal current
            result = original_restore()
            current = None
            return result

        def transition(action, **kwargs):
            nonlocal current
            if current is None:
                current = dict(decisions=0, ticks=0, transitions=[])
                self.attempts.append(current)
            before = world.root_owner_information()
            result = original_transition(action, **kwargs)
            after = world.root_owner_information() if result.terminal_value is None else None
            current["transitions"].append(dict(completed=result.completed, decisions=result.decisions,
                ticks=result.ticks, reason=result.reason, terminal=result.terminal_value,
                before_tick=before.tick, after_tick=None if after is None else after.tick,
                before_sha=before.sha256, after_sha=None if after is None else after.sha256,
                hard_tick=kwargs["hard_tick"], maximum_decisions=kwargs["maximum_decisions"]))
            if result.completed:
                current["decisions"] += result.decisions
                current["ticks"] += result.ticks
            return result

        world.restore_root = restore
        world.transition = transition
        return world


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=int, choices=(3600, 5400), required=True)
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
        if time.monotonic() - started >= 3540:
            raise TimeoutError("bounded extended search acceptance deadline")

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("extended search source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    limits = SearchLimits()
    native = create_search_native(args.port, transport="compact-delta")
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
        factory = HistoryBeliefFactory(lambda: create_search_native(args.belief_port, transport="compact-delta"),
            catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
        while environment.current_tick < args.threshold or rollout.runtime.clock.ready != {0, 1}:
            deadline()
            if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("actual trajectory ended before registered acceptance stage")
            frozen = rollout.freeze_due_information()
            if frozen:
                actions = {owner: inference.sample(info, rollout.generators[owner]) for owner, info in frozen.items()}
                rollout.commit_simultaneous(actions)
                for owner, info in frozen.items():
                    recorders[owner].record_decision(info, actions[owner])
            outcomes = rollout.advance()
            if any(reason != "executed" for reason in outcomes.values()):
                raise ValueError("actual trajectory contains rejected reservation")
            observations = environment.observe_dual(policy_features_only=True)
            for owner in (0, 1):
                recorders[owner].observe_tick(observations[owner])
        print(json.dumps(dict(kind="stage_ready", threshold=args.threshold, owner=args.owner,
            tick=environment.current_tick, seconds=time.monotonic()-started)), flush=True)
        actual_digest = native._request("state-digest")["sha256"]
        with rollout.snapshot(BattleEnvSidecar) as actual_root:
            for altered in (False, True):
                actual_root.restore()
                mutation = perturb_draw(rollout, args.owner) if altered else None
                information = refreshed_information(rollout)[args.owner]
                history = recorders[args.owner].capture(information)
                atomic_json(args.output.with_name(f"history-{int(altered)}.json"), history.to_payload())
                provider = HistorySearchProvider(factory, history=history, check_deadline=deadline)
                audited = BoundaryAuditProvider(provider)
                identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                    checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                    controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
                before = native._request("state-digest")["sha256"]
                print(json.dumps(dict(kind="search_started", owner=args.owner, altered=altered,
                    threshold=args.threshold, tick=information.tick)), flush=True)
                try:
                    search = CompletedBoundarySearch(audited, inference, inference, limits=limits)
                    outcome = search.run(information, seed=78102103, behavior_seed=78102104,
                        codec=ActionCodec(HOG_DECK), identity=identity, deadline=started+3540)
                    if provider.active is not None:
                        raise ValueError("extended search world lease leaked")
                finally:
                    provider.close()
                after = native._request("state-digest")["sha256"]
                if after != before:
                    raise ValueError("extended search changed actual match")
                record = dict(owner=args.owner, altered=altered, threshold=args.threshold, tick=information.tick,
                    fair_sha=information.sha256, history_sha=history.sha256,
                    outcome=dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target),
                    audit=outcome.audit, attempts=audited.attempts, mutation=mutation,
                    actual_digest_before=before, actual_digest_after=after, lease_released=True)
                records.append(record)
                atomic_json(args.output.with_name("progress.json"), dict(records=records))
                print(json.dumps(dict(kind="search_completed", owner=args.owner, altered=altered,
                    threshold=args.threshold, simulations=outcome.result["valid_simulations"],
                    max_decisions=max(attempt["decisions"] for attempt in audited.attempts),
                    max_ticks=max(attempt["ticks"] for attempt in audited.attempts),
                    seconds=outcome.audit["seconds"], timings=outcome.audit["timings"])), flush=True)
            for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts"):
                if records[0][key] != records[1][key]:
                    raise ValueError(f"hidden draw changed full-horizon search {key}")
            if not any(attempt["decisions"] > 2 or attempt["ticks"] > 60 for attempt in records[0]["attempts"]):
                raise ValueError("registered extended horizon was not exercised")
            actual_root.restore()
        if native._request("state-digest")["sha256"] != actual_digest or actual_root.native.handle is not None:
            raise ValueError("actual root restoration or snapshot cleanup failed")
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen model changed")
        atomic_json(args.output, dict(schema="fair-final-native-extended-search.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            threshold=args.threshold, owner=args.owner, limits=asdict(limits), records=records,
            model_unchanged=True, actual_root_restored=True, snapshot_released=True,
            seconds=time.monotonic()-started, exclusions=["general empirical deck prior",
                "native PRNG mutation", "constant-cost per-root independent reconditioning",
                "full-episode search quality", "normal single-ready belief root"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, seconds=time.monotonic()-started, source_sha256=manifest))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
