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
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


ROOT_TICKS = (110, 2598, 5602)
SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def draw_state(raw):
    return [dict(owner=player["owner"],
        hand=deepcopy(player["hand"]), cycle=deepcopy(player["cycle"]), deck=deepcopy(player["deck"]))
        for player in raw["players"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--puct-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    project = Path(__file__).resolve().parents[1]
    identities = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in identities.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("refill probe source changed")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_sha = semantic_digest(source.state_dict())
    trace = json.loads(args.trace.read_text())
    groups = {group["tick"]: group for group in trace["boundaries"]}
    native = create_search_native(args.port, transport="compact-delta")
    changes, roots = [], []
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
        previous = None
        while not rollout.runtime.ended:
            if time.monotonic()-started > 1200:
                raise TimeoutError("bounded refill trajectory deadline")
            if rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("refill trajectory failed or truncated")
            tick = rollout.runtime.clock.tick
            draw = draw_state(environment.raw_observation)
            if draw != previous:
                changes.append(dict(tick=tick, players=draw))
                previous = draw
            frozen = rollout.freeze_due_information()
            if frozen:
                group = groups.pop(tick)
                if {str(owner): info.sha256 for owner, info in frozen.items()} != group["fair_sha"]:
                    raise ValueError(f"refill replay changed original FAIR boundary at {tick}")
                if tick in ROOT_TICKS:
                    for owner, info in frozen.items():
                        atomic_json(args.output.with_name(f"history-{tick}-{owner}.json"),
                            recorders[owner].capture(info).to_payload())
                    digest = native._request("state-digest")["sha256"]
                    continuations = []
                    with rollout.snapshot(BattleEnvSidecar) as snapshot:
                        for repeat in range(2):
                            snapshot.restore()
                            continuation = []
                            for offset in range(30):
                                due = rollout.freeze_due_information()
                                if due:
                                    rollout.commit_simultaneous({owner: FullAction() for owner in due})
                                rollout.advance()
                                continuation.append(dict(tick=rollout.runtime.clock.tick,
                                    players=draw_state(environment.raw_observation),
                                    digest=native._request("state-digest")["sha256"]))
                            continuations.append(continuation)
                        snapshot.restore()
                    if continuations[0] != continuations[1] or native._request("state-digest")["sha256"] != digest:
                        raise ValueError("refill snapshot continuation or restore differs")
                    roots.append(dict(tick=tick, players=draw, continuations=continuations,
                        restored=True, digest=digest, fair_sha=group["fair_sha"]))
                    print(json.dumps(dict(phase="refill-root", tick=tick, roots=len(roots))), flush=True)
                selected = {int(owner): FullAction(**action) for owner, action in group["actions"].items()}
                rollout.commit_simultaneous(selected)
                for owner, info in frozen.items():
                    recorders[owner].record_decision(info, selected[owner])
            outcomes = rollout.advance()
            if any(reason != "executed" for reason in outcomes.values()):
                raise ValueError("refill trajectory rejected an action")
            observations = environment.observe_dual(policy_features_only=True)
            if not rollout.runtime.ended:
                for owner in (0, 1):
                    recorders[owner].observe_tick(observations[owner])
        result = terminal_result(environment.observe_dual(policy_features_only=True)[0].terminal,
            environment.raw_observation, truncated=rollout.runtime.truncated, failed=rollout.runtime.failed)
        if groups or result != trace["result"] or tuple(row["tick"] for row in roots) != ROOT_TICKS:
            raise ValueError("refill trajectory coverage or terminal differs")
        if semantic_digest(source.state_dict()) != model_sha:
            raise ValueError("refill probe changed source model")
        atomic_json(args.output, dict(schema="fair-final-refill-trajectory.v1", status="passed",
            training_admissible=False, source_sha256=identities, checkpoint_sha256=source_sha,
            input_trace_sha256=hashlib.sha256(args.trace.read_bytes()).hexdigest(),
            changes=changes, roots=roots, result=result, seconds=time.monotonic()-started,
            model_unchanged=True, engine_seed=78101906, execution_seed=78101946))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(error=repr(error), roots=roots, changes=changes))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
