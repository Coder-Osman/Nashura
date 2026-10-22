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
from native_runner.training.fair_final.history_provider import HistorySearchProvider, RecoveringHistoryCursor
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from probe_fair_final_visibility import perturb_draw, refreshed_information


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.port == args.belief_port:
        raise ValueError("separate actual and belief engines required")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic() - started >= 1740:
            raise TimeoutError("bounded history search acceptance deadline")

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("history search source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    limits = SearchLimits(soft_decisions=2, soft_ticks=60)
    native = create_search_native(args.port, transport="compact-delta")
    records, recovery = [], []
    cursor = None
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

        def advance_actual():
            deadline()
            if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("actual trajectory ended before acceptance root")
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

        cursor = RecoveringHistoryCursor(factory, seed=78102051, check_deadline=deadline)
        for boundary in range(12):
            frozen = rollout.freeze_due_information()
            history = recorders[0].capture(frozen[0])
            before = native._request("state-digest")["sha256"]
            cursor.sync(history, frozen[0])
            if native._request("state-digest")["sha256"] != before:
                raise ValueError("history recovery modified actual world")
            recovery.append(dict(history_sha=history.sha256, fair_sha=frozen[0].sha256,
                actual_match_unchanged=True, **cursor.events[-1]))
            print(json.dumps(dict(kind="recovery", **recovery[-1])), flush=True)
            advance_actual()
            while rollout.runtime.clock.ready != {0, 1}:
                advance_actual()
        cursor.close()
        cursor = None
        while environment.current_tick < 1200 or rollout.runtime.clock.ready != {0, 1}:
            advance_actual()
        with rollout.snapshot(BattleEnvSidecar) as actual_root:
            for owner in (0, 1):
                outcomes, fair_shas = [], []
                for altered in (False, True):
                    actual_root.restore()
                    mutation = perturb_draw(rollout, owner) if altered else None
                    information = refreshed_information(rollout)[owner]
                    history = recorders[owner].capture(information)
                    atomic_json(args.output.with_name(f"history-root-{owner}-{int(altered)}.json"), history.to_payload())
                    provider = HistorySearchProvider(factory, history=history, check_deadline=deadline)
                    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                        checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
                    before = native._request("state-digest")["sha256"]
                    try:
                        search = CompletedBoundarySearch(provider, inference, inference, limits=limits)
                        outcome = search.run(information, seed=78102103, behavior_seed=78102104,
                            codec=ActionCodec(HOG_DECK), identity=identity, deadline=started+1740)
                        if provider.active is not None:
                            raise ValueError("search world lease leaked")
                    finally:
                        provider.close()
                    if native._request("state-digest")["sha256"] != before:
                        raise ValueError("history search changed actual match")
                    comparable = dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target)
                    outcomes.append(comparable)
                    fair_shas.append(information.sha256)
                    records.append(dict(owner=owner, altered=altered, tick=information.tick,
                        fair_sha=information.sha256, history_sha=history.sha256, outcome=comparable,
                        audit=outcome.audit, mutation=mutation, actual_match_unchanged=True, lease_released=True))
                    atomic_json(args.output.with_name("history-search-progress.json"), dict(records=records, recovery=recovery))
                    print(json.dumps(dict(kind="search", owner=owner, altered=altered, tick=information.tick,
                        simulations=outcome.result["valid_simulations"], seconds=outcome.audit["seconds"])), flush=True)
                if outcomes[0] != outcomes[1] or fair_shas[0] != fair_shas[1]:
                    raise ValueError("actual hidden draw changed conditional history Full128 result")
            actual_root.restore()
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen model parameters or buffers changed")
        atomic_json(args.output, dict(schema="fair-final-native-history-search-acceptance.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            limits=asdict(limits), records=records, recovery=recovery,
            model_unchanged=True, seconds=time.monotonic()-started, exclusions=[
                "default six-decision/200-tick horizon", "late and overtime Full128",
                "native single-ready opponent-pending root", "general empirical deck prior",
                "native PRNG mutation", "constant-cost per-root independent reconditioning"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, recovery=recovery, seconds=time.monotonic()-started, source_sha256=manifest))
        raise
    finally:
        if cursor is not None:
            cursor.close()
        native.close_transport()


if __name__ == "__main__":
    main()
