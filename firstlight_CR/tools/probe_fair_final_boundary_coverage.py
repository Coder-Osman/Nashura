import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.native import FinalBattleEnv, FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_final.visibility import event_visible_to_owner
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def public_executions(events):
    return [event.to_dict() for event in events if event.event_type == "action_executed"
        and event.data.get("private_to") is None
        and all(event_visible_to_owner(event, owner) for owner in (0, 1))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("sampled", "wait"))
    parser.add_argument("--engine-seed", required=True, type=int)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    started = time.monotonic()
    project = Path(__file__).resolve().parents[1]
    manifest = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("boundary coverage source changed")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_sha = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    native = create_search_native(args.port, transport="compact-delta")
    boundaries, ticks = [], []
    atomic_json(args.output.with_name("private-inputs.json"), dict(engine_seed=args.engine_seed, execution_seed=78101946))
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=args.engine_seed,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101946)
        runtime = rollout.runtime
        opening_tick = runtime.clock.tick
        while not runtime.ended and not runtime.truncated:
            if time.monotonic()-started > 1200:
                raise TimeoutError("bounded full-trajectory boundary coverage deadline")
            frozen = rollout.freeze_due_information()
            if frozen:
                if runtime.clock.ready != {0, 1} or runtime.clock.pending:
                    raise ValueError("successful-public-play boundary closure violated")
                selected = {owner: FullAction() if args.mode == "wait" else inference.sample(info, rollout.generators[owner])
                    for owner, info in frozen.items()}
                if any(not frozen[owner].legality.legal(action) for owner, action in selected.items()):
                    raise ValueError("boundary coverage sampled illegal action")
                raw = environment.raw_observation
                boundaries.append(dict(tick=runtime.clock.tick, ready=sorted(runtime.clock.ready), pending=[],
                    fair_sha={str(owner):info.sha256 for owner, info in frozen.items()},
                    draw_sizes=[dict(owner=player["owner"], hand=len(player["hand"]), cycle=len(player["cycle"]))
                        for player in raw["players"]], actions={str(owner):asdict(action) for owner, action in selected.items()},
                    legal=True))
                rollout.commit_simultaneous(selected)
            evidence_offset = len(runtime.clock.evidence)
            outcomes = rollout.advance()
            events = environment.action_outcomes()
            public = public_executions(events)
            evidence = runtime.clock.evidence[evidence_offset:]
            if (any(reason != "executed" for reason in outcomes.values())
                    or any(event.event_type == "action_rejected" for event in events)
                    or any(item["reason"] == "rejected" for item in evidence)):
                raise ValueError("rejection invalidates normal-path coverage")
            if any(item["reason"] == "executed" for item in evidence) and not public:
                raise ValueError("successful ACT lacks public deployment wake evidence")
            ticks.append(dict(tick=runtime.clock.tick, outcomes={str(key):value for key,value in outcomes.items()},
                public_executions=public, ready=sorted(runtime.clock.ready),
                evidence=evidence, ended=runtime.ended, truncated=runtime.truncated,
                actions_open=bool(environment.policy_actions_open)))
            if runtime.clock.tick % 1000 == 0 or runtime.ended:
                atomic_json(args.output.with_name("progress.json"), dict(tick=runtime.clock.tick,
                    boundaries=len(boundaries), seconds=time.monotonic()-started))
                print(json.dumps(dict(tick=runtime.clock.tick, boundaries=len(boundaries))), flush=True)
        observations = environment.observe_dual(policy_features_only=True)
        if observations[0].terminal != observations[1].terminal or any(obs.truncated for obs in observations.values()):
            raise ValueError("native terminal observations disagree")
        result = terminal_result(observations[0].terminal, environment.raw_observation,
            truncated=runtime.truncated, failed=runtime.failed)
        if runtime.clock.pending or runtime.clock.ready or runtime.frozen:
            raise ValueError("terminal has unresolved controller state")
        if semantic_digest(source.state_dict()) != model_sha:
            raise ValueError("boundary audit changed model")
        trace = dict(schema="fair-final-boundary-trajectory.v1", opening_tick=opening_tick,
            boundaries=boundaries, ticks=ticks, final_clock=runtime.clock.capture(), result=result)
        trace_path = args.output.with_name("trace.json")
        atomic_json(trace_path, trace)
        atomic_json(args.output, dict(schema="fair-final-native-boundary-coverage.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            wake_protocol=FinalDecisionRuntime.wake_protocol, mode=args.mode, model_unchanged=True,
            opening_tick=opening_tick, terminal_tick=runtime.clock.tick, result=result,
            boundary_groups=len(boundaries), decisions=2*len(boundaries), single_ready_groups=0,
            incomplete_draw_groups=sum(any(item["hand"] != 4 or item["cycle"] != 4
                for item in row["draw_sizes"]) for row in boundaries),
            lifecycle=dict(Counter(item["reason"] for item in runtime.clock.evidence)),
            trace_sha256=hashlib.sha256(trace_path.read_bytes()).hexdigest(), seconds=time.monotonic()-started,
            exclusions=["arbitrary-wake-protocol", "rejected-action-search-roots", "full-game-search", "training-admission"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            boundaries=boundaries, ticks=ticks, training_admissible=False))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
