import argparse
from dataclasses import asdict, fields
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.adapter import ADAPTER_ID, source_policy
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.recovery import RecoveryJournal, rng_states
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("baseline", "recover"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    started = time.monotonic()
    project = Path(__file__).resolve().parents[1]
    manifest_path = project / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((project / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("deployed recovery source identity differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    identity = dict(checkpoint_sha256=source_sha, engine_sha256=args.engine_sha256,
        source_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        controller=FinalBattleEnv.timing_protocol, adapter=ADAPTER_ID)
    args.output.mkdir(parents=True, exist_ok=True)
    private_root = args.output / "private-audit"
    natives = []
    journal = None

    def factory(inputs, episode_id=None):
        native = create_search_native(args.port, transport="compact-delta")
        natives.append(native)
        environment = FinalBattleEnv(native=native)
        init_fields = {field.name for field in fields(MatchConfig) if field.init}
        environment.reset(match_config=MatchConfig(**{key: value
            for key, value in inputs["match_config"].items() if key in init_fields}),
            options=inputs["reset_options"])
        if episode_id is not None:
            environment.episode_id = episode_id
        native.start_atomic_steps()
        return FinalRollout(environment, catalog=source.policy.backbone.catalog,
            execution_seed=inputs["execution_seed"])

    def select(information, generators):
        selected = {}
        for owner, info in information.items():
            policy, value = source_policy(source, info.frame, info.legality)
            if not torch.isfinite(value).all():
                raise ValueError("source value is not finite")
            selected[owner], _ = policy.sample(generators[owner])
        return selected

    def guard(rollout):
        if time.monotonic() - started > 900:
            raise TimeoutError("bounded recovery probe exceeded wall limit")
        if rollout.runtime.truncated or rollout.runtime.failed:
            raise ValueError("recovery probe failed or truncated")
        if rollout.runtime.clock.tick % 200 == 0:
            atomic_json(args.output / (args.mode + "-progress.json"),
                dict(mode=args.mode, tick=rollout.runtime.clock.tick, seconds=time.monotonic() - started))

    try:
        with torch.inference_mode():
            if args.mode == "baseline":
                config = MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK), seed=78101961,
                    end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())
                inputs = dict(match_config=asdict(config), reset_options={"render_mode": "headless",
                    "decision_ticks": 1, "event_driven_decisions": False,
                    "allow_initial_remaining_runtime_rejections": True}, execution_seed=78101971)
                rollout = factory(inputs)
                journal = RecoveryJournal.create(private_root, rollout, identity=identity, private_inputs=inputs)
                while rollout.runtime.clock.tick < 1200 or rollout.runtime.clock.ready or not rollout.runtime.clock.pending:
                    guard(rollout)
                    if rollout.runtime.ended:
                        raise ValueError("match finished before the recovery probe boundary")
                    if rollout.runtime.clock.ready:
                        journal.commit(select)
                    journal.advance()
                receipt = journal.checkpoint()
                atomic_json(args.output / "recovery-receipt.json", receipt)
                replay_seconds = None
            else:
                receipt = json.loads((args.output / "recovery-receipt.json").read_text())
                before = time.monotonic()
                journal = RecoveryJournal.recover(private_root, factory, expected_identity=identity,
                    expected_manifest_sha=receipt["manifest_sha"])
                replay_seconds = time.monotonic() - before
                if journal.sequence != receipt["records"] or journal.previous_sha != receipt["record_sha"]:
                    raise ValueError("recovery did not reach the externally pinned journal head")
                rollout = journal.rollout
            boundary = dict(tick=rollout.runtime.clock.tick, clock=rollout.runtime.clock.capture(),
                rng_sha=semantic_digest(rng_states(rollout)),
                native_digest=rollout.runtime.environment.native._request("state-digest")["sha256"])
            journal.close()
            suffix = []
            while not rollout.runtime.ended:
                guard(rollout)
                frozen = rollout.freeze_due_information()
                if frozen:
                    actions = select(frozen, rollout.generators)
                    suffix.append(dict(tick=rollout.runtime.clock.tick,
                        fair={str(owner): info.sha256 for owner, info in frozen.items()},
                        actions={str(owner): asdict(action) for owner, action in actions.items()},
                        native_digest=rollout.runtime.environment.native._request("state-digest")["sha256"]))
                    rollout.commit_simultaneous(actions)
                rollout.advance()
            environment = rollout.runtime.environment
            observations = environment.observe_dual(policy_features_only=True)
            if observations[0].terminal != observations[1].terminal:
                raise ValueError("terminal player views differ")
            terminal = terminal_result(observations[0].terminal, environment.raw_observation,
                truncated=rollout.runtime.truncated, failed=rollout.runtime.failed)
            result = dict(boundary=boundary, suffix=suffix, terminal=terminal,
                clock=rollout.runtime.clock.capture(), rng_sha=semantic_digest(rng_states(rollout)),
                native_digest=environment.native._request("state-digest")["sha256"])
            if args.mode == "recover":
                baseline = json.loads((args.output / "baseline.json").read_text())
                if baseline["trajectory"] != result:
                    atomic_json(args.output / "mismatch.json", result)
                    raise ValueError("new-process continuation differs from uninterrupted trajectory")
            report = dict(status="passed", mode=args.mode, training_admissible=False, identity=identity,
                source_sha256=manifest, trajectory=result, seconds=time.monotonic() - started,
                replay_seconds=replay_seconds, suffix_boundaries=len(suffix),
                total_decisions=rollout.runtime.clock.next_identity)
            atomic_json(args.output / (args.mode + ".json"), report)
            print(json.dumps({key: value for key, value in report.items() if key not in
                ("trajectory", "identity", "source_sha256")}), flush=True)
    finally:
        if journal is not None:
            journal.close()
        for native in natives:
            native.close_transport()


if __name__ == "__main__":
    main()
