import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.fair_final_random_census import NativeRandomCensus, semantic_census


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
THRESHOLDS = (100, 1200, 3600, 5400)


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
            raise ValueError("random census deployed source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    started, records = time.monotonic(), []
    native = create_search_native(args.port, transport="compact-delta")
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101971)
        audit = NativeRandomCensus(args.instance, probe=args.probe, libg=args.libg, port=args.port)

        def advance_once():
            if time.monotonic() - started > 900:
                raise TimeoutError("bounded random census deadline")
            if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("random census trajectory ended before all stages")
            information = rollout.freeze_due_information()
            actions = {owner: inference.sample(info, rollout.generators[owner])
                for owner, info in information.items()}
            if actions:
                rollout.commit_simultaneous(actions)
            if any(reason != "executed" for reason in rollout.advance().values()):
                raise ValueError("random census reservation rejected")
            return {str(owner): action.key for owner, action in actions.items()}

        for threshold in THRESHOLDS:
            while environment.current_tick < threshold or rollout.runtime.clock.ready != {0, 1}:
                advance_once()
            before_digest = native._request("state-digest")["sha256"]
            before_raw = native.observe()
            before = audit.inspect(before_raw)
            after_digest = native._request("state-digest")["sha256"]
            if before_raw != native.observe() or before_digest != after_digest:
                raise ValueError("readonly census changed native state")
            rejected = []
            for field in ("generation", "stateEpoch", "tick"):
                try:
                    audit.inspect({**before_raw, field: before_raw[field] + 1})
                except ValueError as error:
                    if "stale or active" not in str(error):
                        raise
                    rejected.append(field)
                else:
                    raise ValueError("stale census accepted")
            continuations = []
            with rollout.snapshot(BattleEnvSidecar) as snapshot:
                for repeat in range(2):
                    if repeat:
                        snapshot.restore()
                    actions = advance_once()
                    next_digest = native._request("state-digest")["sha256"]
                    continuations.append(dict(actions=actions, digest=next_digest,
                        census=audit.inspect(native.observe())))
                snapshot.restore()
                restored_digest = native._request("state-digest")["sha256"]
                restored = audit.inspect(native.observe())
            if semantic_census(before) != semantic_census(restored) or before_digest != restored_digest:
                raise ValueError("random census snapshot restoration differs")
            comparable = [{**item, "census": semantic_census(item["census"])} for item in continuations]
            if comparable[0] != comparable[1] or snapshot.native.handle is not None:
                raise ValueError("random census continuation differs or snapshot leaked")
            records.append(dict(threshold=threshold, before=before, restored=restored,
                before_digest=before_digest, after_read_digest=after_digest, restored_digest=restored_digest,
                continuations=continuations, raw_unchanged=True, stale_rejected=rejected, snapshot_released=True))
            atomic_json(args.output.with_name("progress.json"), dict(records=records))
            print(json.dumps(dict(kind="census_stage_passed", threshold=threshold, state=before)), flush=True)
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("random census changed source model")
        atomic_json(args.output, dict(schema="fair-final-native-random-census.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            records=records, model_unchanged=True, seconds=time.monotonic() - started,
            exclusions=["complete random object coverage", "Full128 hidden-random noninterference",
                "component random state ownership"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error), records=records))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
