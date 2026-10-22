import argparse
from dataclasses import fields, is_dataclass
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.history_belief import HistoryBeliefFactory
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


def frame_differences(left, right, prefix="frame"):
    if is_dataclass(left) and is_dataclass(right):
        return [item for field in fields(left) for item in frame_differences(
            getattr(left, field.name), getattr(right, field.name), prefix + "." + field.name)]
    if isinstance(left, torch.Tensor) and isinstance(right, torch.Tensor):
        if left.shape != right.shape:
            return [dict(field=prefix, expected_shape=list(left.shape), actual_shape=list(right.shape))]
        if not torch.equal(left, right):
            changed = left != right
            return [dict(field=prefix, changed=int(changed.sum()),
                expected=left[changed].tolist()[:12], actual=right[changed].tolist()[:12])]
    elif semantic_digest(left) != semantic_digest(right):
        return [dict(field=prefix, expected_sha=semantic_digest(left), actual_sha=semantic_digest(right))]
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thresholds", type=int, nargs="+", default=[180, 1200, 3600, 5400])
    parser.add_argument("--incremental", action="store_true")
    args = parser.parse_args()
    if args.port == args.belief_port or args.thresholds != sorted(set(args.thresholds)):
        raise ValueError("separate engine ports and ascending stage thresholds required")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("history probe source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a")), source_adapter=True)
    native = create_search_native(args.port, transport="compact-delta")
    session = None
    records = []

    def deadline():
        if time.monotonic() - started > 1500:
            raise TimeoutError("bounded history probe deadline")

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

        def advance_actual(frozen):
            if frozen:
                selected = {owner: inference.sample(info, rollout.generators[owner]) for owner, info in frozen.items()}
                rollout.commit_simultaneous(selected)
                for owner, info in frozen.items():
                    recorders[owner].record_decision(info, selected[owner])
            outcomes = rollout.advance()
            if any(reason != "executed" for reason in outcomes.values()):
                raise ValueError("actual reservation rejected")
            observations = environment.observe_dual(policy_features_only=True)
            for owner in (0, 1):
                recorders[owner].observe_tick(observations[owner])

        stages = list(args.thresholds)
        while stages:
            deadline()
            if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("actual match ended before history probe coverage")
            frozen = rollout.freeze_due_information()
            if rollout.runtime.clock.ready == {0, 1} and environment.current_tick >= stages[0]:
                threshold = stages.pop(0)
                base_information = frozen
                histories = {owner: recorders[owner].capture(info) for owner, info in frozen.items()}
                if args.incremental:
                    advance_actual(frozen)
                    while rollout.runtime.clock.ready != {0, 1}:
                        deadline()
                        if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                            raise ValueError("actual match ended before incremental boundary")
                        advance_actual(rollout.freeze_due_information())
                    frozen = rollout.freeze_due_information()
                next_histories = {owner: recorders[owner].capture(info) for owner, info in frozen.items()}
                for owner in (0, 1):
                    info = base_information[owner]
                    history = histories[owner]
                    atomic_json(args.output.with_name(f"history-{threshold}-{owner}.json"), history.to_payload())
                    before = native._request("state-digest")["sha256"]
                    session = factory.build(history, info, seed=78102031 + owner, attempts=1, check_deadline=deadline)
                    cold_profile = dict(session.profile)
                    if args.incremental:
                        atomic_json(args.output.with_name(f"history-{threshold}-{owner}-incremental.json"),
                            next_histories[owner].to_payload())
                        session.sync(next_histories[owner], frozen[owner], check_deadline=deadline)
                    if native._request("state-digest")["sha256"] != before:
                        raise ValueError("belief reconstruction modified actual match")
                    records.append(dict(threshold=threshold, tick=info.tick, owner=owner,
                        exact=True, own_decisions=len(history.decisions), public_deployments=len(history.deployments),
                        history_sha=history.sha256, profile=dict(session.profile),
                        cold_profile=cold_profile, incremental=args.incremental, next_tick=frozen[owner].tick,
                        next_fair_sha=frozen[owner].sha256, actual_match_unchanged=True))
                    print(json.dumps(records[-1]), flush=True)
                    session.close()
                    session.rollout.runtime.environment.native.close_transport()
                    session = None
                atomic_json(args.output.with_name("history-progress.json"), dict(checks=records))
            if not stages:
                break
            advance_actual(frozen)
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen model changed during history probe")
        atomic_json(args.output, dict(schema="fair-final-native-history-acceptance.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            records=records, seconds=time.monotonic()-started, model_unchanged=True,
            exclusions=["general empirical deck prior", "full-episode search quality", "native PRNG mutation"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed",
            error=repr(error), records=records, seconds=time.monotonic()-started, source_sha256=manifest))
        raise
    finally:
        if session is not None:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
        native.close_transport()


if __name__ == "__main__":
    main()
