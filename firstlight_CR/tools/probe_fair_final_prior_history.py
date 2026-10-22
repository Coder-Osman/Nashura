import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.prior_history import PriorHistoryBeliefFactory
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search_runtime import FrozenSearchInference, RolloutSearchWorld
from native_runner.training.fair_search.belief import initial_draw
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from probe_fair_final_visibility import perturb_draw, refreshed_information


THRESHOLDS = (1200, 3600)
SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.port == args.belief_port:
        raise ValueError("independent native ports required")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic()-started > 1500:
            raise TimeoutError("bounded prior integration deadline")

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("prior probe source differs")
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a")), source_adapter=True)
    native = create_search_native(args.port, transport="compact-delta")
    factory = PriorHistoryBeliefFactory(lambda:create_search_native(args.belief_port, transport="compact-delta"),
        prior=prior, catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    session, world = None, None
    records, incremental = [], []
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101971)
        initial = rollout.freeze_due_information()
        recorders = {owner:FairHistoryRecorder(rollout.runtime.frozen[owner], info,
            opening_tick=environment.warmup_ticks) for owner, info in initial.items()}

        def advance_actual():
            deadline()
            if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("actual trajectory ended before registered coverage")
            information = rollout.freeze_due_information()
            if information:
                actions = {owner:inference.sample(info, rollout.generators[owner]) for owner, info in information.items()}
                rollout.commit_simultaneous(actions)
                for owner, info in information.items():
                    recorders[owner].record_decision(info, actions[owner])
            outcomes = rollout.advance()
            if any(reason != "executed" for reason in outcomes.values()):
                raise ValueError("actual trajectory rejected a reservation")
            observations = environment.observe_dual(policy_features_only=True)
            for owner in (0, 1):
                recorders[owner].observe_tick(observations[owner])

        for threshold in THRESHOLDS:
            while environment.current_tick < threshold or rollout.runtime.clock.ready != {0, 1}:
                advance_actual()
            information = rollout.freeze_due_information()
            histories = {owner:recorders[owner].capture(info) for owner, info in information.items()}
            base_digest = native._request("state-digest")["sha256"]
            for owner in (0, 1):
                history = histories[owner]
                atomic_json(args.output.with_name(f"history-{threshold}-{owner}.json"), history.to_payload())
                paired = []
                with rollout.snapshot(BattleEnvSidecar) as actual_root:
                    for variant in ("original", "hidden-draw-changed"):
                        actual_root.restore()
                        mutation = None if variant == "original" else perturb_draw(rollout, owner)
                        info = refreshed_information(rollout)[owner]
                        if info.sha256 != information[owner].sha256 or recorders[owner].capture(info) != history:
                            raise ValueError("hidden intervention changed supplied FAIR root/history")
                        actual_before = native._request("state-digest")["sha256"]
                        session = factory.build(history, info, seed=78102031+owner, attempts=4, check_deadline=deadline)
                        evidence = deepcopy(session.conditioning_manifest)
                        completion = session.draw_completion
                        root_raw = session.rollout.runtime.environment.native._request("state-digest")["sha256"]
                        world = RolloutSearchWorld(session.rollout, owner=owner,
                            sidecar_factory=BattleEnvSidecar, manifest=evidence)
                        signatures = []
                        for repeat in range(2):
                            world.restore_root()
                            transition = world.transition(FullAction(), opponent=inference,
                                generator=torch.Generator().manual_seed(78102119),
                                hard_tick=info.tick+120, maximum_decisions=6, check_deadline=deadline)
                            if not transition.completed:
                                raise ValueError("diagnostic WAIT edge did not reach completed boundary")
                            signatures.append(dict(completed=True, ticks=transition.ticks, decisions=transition.decisions,
                                reason=transition.reason, terminal_value=transition.terminal_value,
                                fair_sha=None if transition.information is None else transition.information.sha256,
                                native_digest=session.rollout.runtime.environment.native._request("state-digest")["sha256"]))
                        world.close()
                        world = None
                        restored = session.rollout.runtime.environment.native._request("state-digest")["sha256"]
                        if signatures[0] != signatures[1] or restored != root_raw:
                            raise ValueError("prior root continuation or snapshot restoration differs")
                        if native._request("state-digest")["sha256"] != actual_before:
                            raise ValueError("independent prior world changed actual match")
                        paired.append(dict(manifest=evidence, initial_draw=completion.initial_draw,
                            current_draw=completion.current_draw, native_digest=root_raw, continuation=signatures[0]))
                        records.append(dict(threshold=threshold, owner=owner, variant=variant, tick=info.tick,
                            history_sha=history.sha256, fair_sha=info.sha256, conditioning=evidence,
                            root_native_digest=root_raw, mutation=mutation, continuations=signatures,
                            root_restored=True, actual_match_unchanged=True, profile=deepcopy(session.profile),
                            cycle_delta=deepcopy(factory.cursors[owner].last_delta),
                            initial_draw=list(completion.initial_draw), current_draw=list(completion.current_draw),
                            snapshot_released=True))
                        session.close()
                        session.rollout.runtime.environment.native.close_transport()
                        session = None
                        print(json.dumps(dict(phase="paired-root", threshold=threshold, owner=owner,
                            variant=variant, tick=info.tick, elapsed=time.monotonic()-started)), flush=True)
                        atomic_json(args.output.with_name("progress.json"), dict(records=records, incremental=incremental))
                    if paired[0] != paired[1]:
                        raise ValueError("actual hidden draw changed independently sampled prior world")
                    actual_root.restore()
                if native._request("state-digest")["sha256"] != base_digest:
                    raise ValueError("actual stage root did not restore")
            advance_actual()
            while rollout.runtime.clock.ready != {0, 1}:
                advance_actual()
            following = rollout.freeze_due_information()
            for owner in (0, 1):
                history = recorders[owner].capture(following[owner])
                atomic_json(args.output.with_name(f"history-{threshold}-{owner}-next.json"), history.to_payload())
                cursor = factory.cursors[owner]
                delta = cursor.sync(history)
                sampled = cursor.sample(history, seed=78102131+owner)
                from native_runner.training.fair_final.prior_history import HistoryCycleCursor
                cold = HistoryCycleCursor(prior, opening=history.opening)
                if sampled != cold.sample(history, seed=78102131+owner):
                    raise ValueError("successor cycle cache differs from cold prior completion")
                incremental.append(dict(threshold=threshold, owner=owner, tick=history.root_tick,
                    history_sha=history.sha256, delta=delta, completion_manifest=sampled.manifest, exact=True))
            atomic_json(args.output.with_name("progress.json"), dict(records=records, incremental=incremental))
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen source parameters or buffers changed")
        atomic_json(args.output, dict(schema="fair-final-native-prior-history.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            prior_sha=prior.sha256, prior_file_sha=hashlib.sha256(args.prior.read_bytes()).hexdigest(),
            sampler_sha=factory.sampler_sha, records=records, incremental=incremental,
            model_unchanged=True, seconds=time.monotonic()-started, thresholds=list(THRESHOLDS),
            exclusions=["Full128-search", "native-local-reconditioning", "population-prior-calibration",
                "normal-single-ready-root", "training-admission", "end-to-end-throughput"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, incremental=incremental, seconds=time.monotonic()-started))
        raise
    finally:
        if world is not None:
            world.close()
        if session is not None:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
        factory.close()
        native.close_transport()


if __name__ == "__main__":
    main()
