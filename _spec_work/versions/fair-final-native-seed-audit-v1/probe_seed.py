import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from seed_audit import NativeSeedAudit
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.transport import create_search_native
from native_runner.training.v4.factory import production_semantic_bundle


THRESHOLDS = (90, 1200, 3600, 5400)


def semantic(record):
    return {name: value for name, value in record.items() if name not in ("generation", "stateEpoch")}


def reset_transport(native, port, config, options):
    native.close_transport()
    replacement = create_search_native(port, transport="compact-delta")
    try:
        environment = FinalBattleEnv(native=replacement)
        environment.reset(match_config=config, options=options)
        replacement.start_atomic_steps()
    except BaseException:
        replacement.close_transport()
        raise
    return replacement, environment


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "probe", "libg", "puct-root", "output", "project"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delayed", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar
    identities = json.loads((args.project / "probe-source-manifest.json").read_text())
    for name, expected in identities.items():
        if hashlib.sha256((args.project / name).read_bytes()).hexdigest() != expected:
            raise ValueError("seed probe frozen runtime differs")
    native = create_search_native(args.port, transport="compact-delta")
    records, started = [], time.monotonic()
    try:
        audit = NativeSeedAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port)
        startup = audit.inspect()
        if startup["base_time"] == 0:
            raise ValueError("native seed base time uninitialized")
        if args.delayed:
            while time.time() <= startup["base_time"] + 16:
                if time.monotonic() - started > 25:
                    raise TimeoutError("seed delayed startup bound exceeded")
                time.sleep(.1)
        before_reset = audit.inspect()
        reset_wall_time = time.time()
        environment = FinalBattleEnv(native=native)
        config = MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())
        options = dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
            allow_initial_remaining_runtime_rejections=True)
        environment.reset(match_config=config, options=options)
        native.start_atomic_steps()
        initial = dict(digest=native._request("state-digest")["sha256"], census=audit.inspect(native.observe()))
        rollout = FinalRollout(environment, catalog=production_semantic_bundle().card_catalog,
            execution_seed=78101971)
        def advance():
            if time.monotonic() - started > 300:
                raise TimeoutError("bounded native seed audit deadline")
            if rollout.runtime.failed or rollout.runtime.ended or rollout.runtime.truncated:
                raise ValueError("native seed trajectory ended before all stages")
            information = rollout.freeze_due_information()
            if information:
                rollout.commit_simultaneous({owner: FullAction() for owner in information})
            rollout.advance()
        for threshold in THRESHOLDS:
            while environment.current_tick < threshold:
                advance()
            before = native._request("state-digest")["sha256"]
            census = audit.inspect(native.observe())
            if before != native._request("state-digest")["sha256"]:
                raise ValueError("readonly seed census changed native digest")
            stale = []
            for name in ("generation", "stateEpoch", "tick"):
                identity = native.observe()
                identity[name] += 1
                try:
                    audit.inspect(identity)
                except ValueError as error:
                    if "stale or active" not in str(error):
                        raise
                    stale.append(name)
                else:
                    raise ValueError("stale native seed census accepted")
            continuations = []
            with rollout.snapshot(BattleEnvSidecar) as snapshot:
                for repeat in range(2):
                    if repeat:
                        snapshot.restore()
                    advance()
                    continuations.append(dict(digest=native._request("state-digest")["sha256"],
                        census=semantic(audit.inspect(native.observe()))))
                snapshot.restore()
                restored = audit.inspect(native.observe())
                restored_digest = native._request("state-digest")["sha256"]
            if (continuations[0] != continuations[1] or semantic(census) != semantic(restored)
                    or before != restored_digest or snapshot.native.handle is not None):
                raise ValueError("native seed restore or continuation differs")
            records.append(dict(threshold=threshold, census=census, restored=restored,
                digest=before, restored_digest=restored_digest, continuations=continuations,
                stale_rejected=stale, snapshot_released=True))
            atomic_json(args.output.with_name("progress.json"), dict(records=records))
        native, environment = reset_transport(native, args.port, config, options)
        repeated = dict(digest=native._request("state-digest")["sha256"], census=audit.inspect(native.observe()))
        if initial["digest"] != repeated["digest"] or semantic(initial["census"]) != semantic(repeated["census"]):
            raise ValueError("native seed repeated same-config reset differs")
        atomic_json(args.output, dict(schema="fair-final-native-seed-probe.v1", status="passed_limited_scope",
            delayed=args.delayed, startup=startup, before_reset=before_reset, reset_wall_time=reset_wall_time,
            initial=initial, repeated_reset=repeated, records=records, source_sha256=identities,
            behavior="legal-WAIT-only", all_global_writers_proven=False, actual_policy_search_verified=False,
            hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False,
            seconds=time.monotonic() - started))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(error=repr(error), records=records))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
