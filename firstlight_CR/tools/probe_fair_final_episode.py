import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.adapter import ADAPTER_ID, source_policy
from native_runner.training.fair_final.episode import AcceptanceRecorder, atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--attempt-id", type=int, default=1)
    parser.add_argument("--engine-seed", type=int, required=True)
    parser.add_argument("--execution-seed", type=int, required=True)
    parser.add_argument("--wall-seconds", type=float, default=1200)
    parser.add_argument("--wait-only", action="store_true")
    args = parser.parse_args()
    torch.set_num_threads(1)
    started = time.monotonic()
    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("deployed episode source identity differs")
    source, _, identity = load_checkpoint(args.source, device="cuda:0",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    recorder = AcceptanceRecorder(args.output, match_id=args.match_id, attempt_id=args.attempt_id,
        identity=dict(checkpoint_sha256=identity, engine_sha256=args.engine_sha256,
            source_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            controller=FinalBattleEnv.timing_protocol, adapter="wait-only-draw-probe" if args.wait_only else ADAPTER_ID))
    audit = args.output / "audit" / args.match_id / str(args.attempt_id)
    audit.mkdir(parents=True, exist_ok=False)
    atomic_json(audit / "private-inputs.json", dict(engine_seed=args.engine_seed, execution_seed=args.execution_seed))
    progress_path = recorder.directory / "progress.json"
    native = None
    rollout = None
    timings = Counter()
    try:
        native = create_search_native(args.port, transport="compact-delta")
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
            seed=args.engine_seed, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options={"render_mode": "headless", "decision_ticks": 1,
                "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=args.execution_seed)
        with torch.inference_mode():
            while not rollout.runtime.ended and not rollout.runtime.truncated:
                if time.monotonic() - started > args.wall_seconds:
                    raise TimeoutError("complete-episode wall limit exceeded")
                before = time.monotonic()
                frozen = rollout.freeze_due_information()
                timings["freeze_seconds"] += time.monotonic() - before
                if frozen:
                    selected = {}
                    before = time.monotonic()
                    for owner, info in frozen.items():
                        if args.wait_only:
                            selected[owner] = FullAction()
                        else:
                            policy, value = source_policy(source, info.frame, info.legality)
                            if not torch.isfinite(value).all():
                                raise ValueError("nonfinite source value")
                            selected[owner], _ = policy.sample(rollout.generators[owner])
                    timings["inference_seconds"] += time.monotonic() - before
                    rollout.commit_simultaneous(selected)
                    recorder.record_committed(frozen, selected, rollout.runtime.clock)
                before = time.monotonic()
                rollout.advance()
                recorder.reconcile(rollout.runtime.clock)
                timings["advance_seconds"] += time.monotonic() - before
                if environment.current_tick % 100 == 0 or rollout.runtime.ended:
                    atomic_json(progress_path, dict(status="running", tick=environment.current_tick,
                        rows=len(recorder.rows), resolved=recorder.resolved, seconds=time.monotonic() - started))
        observations = environment.observe_dual(policy_features_only=True)
        if any(observation.truncated for observation in observations.values()):
            raise ValueError("terminal observation contains truncation")
        if observations[0].terminal != observations[1].terminal:
            raise ValueError("terminal views disagree")
        raw = environment.raw_observation
        atomic_json(audit / "native-terminal.json", raw)
        result = recorder.publish(terminal=observations[0].terminal, raw=raw, runtime=rollout.runtime)
        report = dict(status="passed", match_id=args.match_id, attempt_id=args.attempt_id,
            terminal=observations[0].terminal.to_dict(), seconds=time.monotonic() - started,
            rows=len(recorder.rows), lifecycle=dict(Counter(row["outcome"]["reason"] for row in recorder.rows)),
            timing=dict(timings), training_admissible=False, publication=result, source_sha256=manifest)
        atomic_json(recorder.directory / "report.json", report)
        atomic_json(progress_path, dict(status="passed", tick=environment.current_tick,
            rows=len(recorder.rows), seconds=report["seconds"]))
        print(json.dumps(report), flush=True)
    except Exception as error:
        if not recorder.sealed:
            recorder.quarantine(f"{type(error).__name__}: {error}")
        atomic_json(progress_path, dict(status="failed", error=f"{type(error).__name__}: {error}",
            tick=None if rollout is None else rollout.runtime.clock.tick, rows=len(recorder.rows),
            seconds=time.monotonic() - started))
        raise
    finally:
        if native is not None:
            native.close_transport()


if __name__ == "__main__":
    main()
