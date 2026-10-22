import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.fair_final_action_random import NativeActionRandomAudit
from tools.fair_final_random_census import semantic_census
from tools.probe_fair_final_random_census import SOURCE_SHA, THRESHOLDS


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "probe", "libg", "source", "puct-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("action random deployed source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    started, samples, stages, decisions = time.monotonic(), [], [], []
    native = create_search_native(args.port, transport="compact-delta")
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101971)
        audit = NativeActionRandomAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port)
        while True:
            if time.monotonic() - started > 1200:
                raise TimeoutError("bounded action random deadline")
            if rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("action random trajectory rejected or truncated")
            due = bool(rollout.runtime.clock.ready)
            inspect_now = due or environment.current_tick in THRESHOLDS or rollout.runtime.ended
            if inspect_now:
                sample = audit.inspect(native.observe())
                samples.append(sample)
                if sample["matches"]:
                    raise ValueError("target random action present; preserve evidence before mutation planning")
            if len(stages) < len(THRESHOLDS) and environment.current_tick == THRESHOLDS[len(stages)]:
                before_digest = native._request("state-digest")["sha256"]
                raw = native.observe()
                current = audit.inspect(raw)
                after_digest = native._request("state-digest")["sha256"]
                if before_digest != after_digest or raw != native.observe() or current != sample:
                    raise ValueError("readonly action inventory changed native observation")
                with rollout.snapshot(BattleEnvSidecar) as snapshot:
                    snapshot.restore()
                    restored = audit.inspect(native.observe())
                    restored_digest = native._request("state-digest")["sha256"]
                if (semantic_census(sample) != semantic_census(restored)
                        or before_digest != restored_digest or snapshot.native.handle is not None):
                    raise ValueError("action inventory root restoration differs")
                stages.append(dict(threshold=THRESHOLDS[len(stages)], sample=sample, restored=restored,
                    before_digest=before_digest, after_digest=after_digest, restored_digest=restored_digest,
                    snapshot_released=True, raw_unchanged=True))
                atomic_json(args.output.with_name("progress.json"), dict(stages=stages, sampled_ticks=len(samples)))
                print(json.dumps(dict(kind="action_inventory_stage", tick=sample["tick"],
                    sampled_ticks=len(samples), runtime_count=sample["runtime_count"])), flush=True)
            if rollout.runtime.ended:
                break
            information = rollout.freeze_due_information()
            actions = {owner: inference.sample(info, rollout.generators[owner])
                for owner, info in information.items()}
            if actions:
                rollout.commit_simultaneous(actions)
                decisions.extend(dict(owner=owner, tick=info.tick, action=actions[owner].key)
                    for owner, info in information.items())
            if any(reason != "executed" for reason in rollout.advance().values()):
                raise ValueError("action random reservation rejected")
        terminal = terminal_result(environment.observe_dual(policy_features_only=True)[0].terminal,
            environment.raw_observation, truncated=rollout.runtime.truncated, failed=rollout.runtime.failed)
        if len(stages) != 4 or semantic_digest(source.state_dict()) != model_before:
            raise ValueError("action random stage coverage or frozen model differs")
        atomic_json(args.output.with_name("samples.json"), dict(samples=samples, decisions=decisions))
        atomic_json(args.output, dict(schema="fair-final-native-action-random.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            stages=stages, terminal=terminal, sampled_ticks=len(samples), decisions=len(decisions),
            matched_instances=sum(len(sample["matches"]) for sample in samples),
            runtime_observations=sum(sample["runtime_count"] for sample in samples),
            start_tick=samples[0]["tick"], final_tick=samples[-1]["tick"], model_unchanged=True,
            seconds=time.monotonic()-started,
            sampling="every-decision-plus-stage-and-terminal.v1",
            exclusions=["between-sample or within-tick transient actions", "other cards or seeds", "all random sources", "target-state snapshot safety"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error), stages=stages))
        atomic_json(args.output.with_name("samples.json"), dict(samples=samples, decisions=decisions))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
