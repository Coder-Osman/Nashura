import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from global_rng_audit import NativeGlobalRngAudit, classify_restore, perturb_state
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.transport import create_search_native
from native_runner.training.v4.factory import production_semantic_bundle


THRESHOLDS = (90, 1200, 3600, 5400)


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "probe", "libg", "puct-root", "output", "project"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delayed", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    sources = json.loads((args.project / "probe-source-manifest.json").read_text())
    for name, expected in sources.items():
        if hashlib.sha256((args.project / name).read_bytes()).hexdigest() != expected:
            raise ValueError("global RNG probe frozen runtime differs")
    native = create_search_native(args.port, transport="compact-delta")
    records, started = [], time.monotonic()
    try:
        audit = NativeGlobalRngAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port)
        startup_bytes, startup = audit.global_state()
        environment = FinalBattleEnv(native=native)
        config = MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())
        options = dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
            allow_initial_remaining_runtime_rejections=True)
        environment.reset(match_config=config, options=options)
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=production_semantic_bundle().card_catalog,
            execution_seed=78101971)
        def advance():
            if time.monotonic() - started > 300:
                raise TimeoutError("bounded native global RNG deadline")
            if rollout.runtime.failed or rollout.runtime.ended or rollout.runtime.truncated:
                raise ValueError("global RNG trajectory ended before all stages")
            information = rollout.freeze_due_information()
            if information:
                rollout.commit_simultaneous({owner: FullAction() for owner in information})
            rollout.advance()
        for threshold in THRESHOLDS:
            while environment.current_tick < threshold:
                advance()
            before_digest = native._request("state-digest")["sha256"]
            original, before = audit.global_state(native.observe())
            if native._request("state-digest")["sha256"] != before_digest:
                raise ValueError("readonly global RNG census changed digest")
            stale = []
            for name in ("generation", "stateEpoch", "tick"):
                identity = native.observe()
                identity[name] += 1
                try:
                    audit.global_state(identity)
                except ValueError as error:
                    if "stale or active" not in str(error):
                        raise
                    stale.append(name)
                else:
                    raise ValueError("stale global RNG census accepted")
            perturbed = perturb_state(original)
            with rollout.snapshot(BattleEnvSidecar) as snapshot:
                restored_bytes = None
                try:
                    audit.global_state(native.observe(), expected=original, replacement=perturbed)
                    perturbed_digest = native._request("state-digest")["sha256"]
                    snapshot.restore()
                    restored_bytes, restored = audit.global_state(native.observe())
                    classification = classify_restore(original, perturbed, restored_bytes)
                finally:
                    current, _ = audit.global_state(native.observe())
                    if current != original:
                        audit.global_state(native.observe(), expected=current, replacement=original)
                repaired_bytes, repaired = audit.global_state(native.observe())
                if repaired_bytes != original:
                    raise ValueError("global RNG repair differs")
                snapshot.restore()
            if snapshot.native.handle is not None or native._request("state-digest")["sha256"] != before_digest:
                raise ValueError("global RNG snapshot release or native digest differs")
            records.append(dict(threshold=threshold, before=before, restored=restored, repaired=repaired,
                original_hex=original.hex(), perturbed_hex=perturbed.hex(), restored_hex=restored_bytes.hex(),
                classification=classification, before_digest=before_digest, perturbed_digest=perturbed_digest,
                final_digest=native._request("state-digest")["sha256"], stale_rejected=stale,
                snapshot_released=True, no_native_steps_while_perturbed=True))
            atomic_json(args.output.with_name("progress.json"), dict(records=records))
        atomic_json(args.output, dict(schema="fair-final-native-global-rng-probe.v1", status="completed_diagnostic",
            second_process=args.delayed, startup=startup, startup_hex=startup_bytes.hex(), records=records,
            source_sha256=sources, behavior="legal-WAIT-with-isolated-global-perturbation-no-step",
            fixed_hog_usage_proven=False, hidden_random_coverage_closed=False,
            training_admissible=False, gates_closed=False, seconds=time.monotonic() - started))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(error=repr(error), records=records))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
