import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from global_rng_audit import LIBG_SHA, NativeGlobalRngAudit, perturb_state
from seed_audit import parse_symbols
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.transport import create_search_native
from native_runner.training.v4.factory import production_semantic_bundle


SCOPE = "native-payload+global-mt2504.v1"
THRESHOLDS = (90, 1200, 3600, 5400)


class SnapshotGlobalAudit(NativeGlobalRngAudit):
    def __init__(self, instance, *, probe, libg, port, probe_sha):
        self.instance = Path(instance).resolve()
        self.probe, self.libg = Path(probe).resolve(), Path(libg).resolve()
        self.record = json.loads((self.instance / "process.json").read_text())
        self.pid, self.port = int(self.record["child_pid"]), port
        for path, expected in ((self.probe, probe_sha), (self.libg, LIBG_SHA)):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("global snapshot probe binary identity differs")
        result = subprocess.run(["readelf", "-sW", str(self.probe)], check=True,
            capture_output=True, text=True, timeout=10)
        self.symbols = parse_symbols(result.stdout)
        self._verify_process()


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "probe", "libg", "puct-root", "output", "project"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delayed", action="store_true")
    args = parser.parse_args()
    settings = json.loads((Path(__file__).parent / "settings.json").read_text())
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    sources = json.loads((args.project / "probe-source-manifest.json").read_text())
    for name, expected in sources.items():
        if hashlib.sha256((args.project / name).read_bytes()).hexdigest() != expected:
            raise ValueError("global snapshot frozen runtime differs")
    native = create_search_native(args.port, transport="compact-delta")
    records, started = [], time.monotonic()
    try:
        audit = SnapshotGlobalAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port,
            probe_sha=settings["engine_sha256"])
        startup_bytes, startup = audit.global_state()
        environment = FinalBattleEnv(native=native)
        config = MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())
        environment.reset(match_config=config, options=dict(render_mode="headless", decision_ticks=1,
            event_driven_decisions=False, allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=production_semantic_bundle().card_catalog,
            execution_seed=78101971)

        def digest():
            result = native._request("state-digest")
            if result.get("scope") != SCOPE:
                raise ValueError("global snapshot digest scope differs")
            return {name: result[name] for name in ("tick", "sha256", "nativeSha256", "scope")}

        def advance():
            if time.monotonic() - started > 300:
                raise TimeoutError("bounded global snapshot deadline")
            if rollout.runtime.failed or rollout.runtime.ended or rollout.runtime.truncated:
                raise ValueError("global snapshot trajectory ended before all stages")
            information = rollout.freeze_due_information()
            if information:
                rollout.commit_simultaneous({owner: FullAction() for owner in information})
            rollout.advance()

        for threshold in THRESHOLDS:
            while environment.current_tick < threshold:
                advance()
            original, before = audit.global_state(native.observe())
            before_digest = digest()
            perturbed = perturb_state(original)
            with rollout.snapshot(BattleEnvSidecar) as snapshot:
                try:
                    audit.global_state(native.observe(), expected=original, replacement=perturbed)
                    perturbed_digest = digest()
                    if (perturbed_digest["sha256"] == before_digest["sha256"]
                            or perturbed_digest["nativeSha256"] != before_digest["nativeSha256"]):
                        raise ValueError("global snapshot digest failed to distinguish global-only mutation")
                    snapshot.restore()
                    restored_bytes, restored = audit.global_state(native.observe())
                    if restored_bytes != original or digest() != before_digest:
                        raise ValueError("global snapshot did not restore every byte and digest")
                    branches = []
                    for _ in range(2):
                        branch = []
                        for _ in range(10):
                            advance()
                            branch.append(digest())
                        branches.append(branch)
                        snapshot.restore()
                    if branches[0] != branches[1]:
                        raise ValueError("global snapshot repeated WAIT continuation differs")
                    repeated_bytes, repeated = audit.global_state(native.observe())
                    if repeated_bytes != original or digest() != before_digest:
                        raise ValueError("global snapshot repeated restore differs")
                finally:
                    current, _ = audit.global_state(native.observe())
                    if current != original:
                        audit.global_state(native.observe(), expected=current, replacement=original)
            if snapshot.native.handle is not None or digest() != before_digest:
                raise ValueError("global snapshot release or final digest differs")
            records.append(dict(threshold=threshold, before=before, restored=restored, repeated=repeated,
                original_hex=original.hex(), perturbed_hex=perturbed.hex(), restored_hex=restored_bytes.hex(),
                repeated_hex=repeated_bytes.hex(), before_digest=before_digest, perturbed_digest=perturbed_digest,
                final_digest=digest(), branches=branches, snapshot_released=True,
                no_native_steps_while_perturbed=True))
            atomic_json(args.output.with_name("progress.json"), dict(records=records))
        atomic_json(args.output, dict(schema="fair-final-global-rng-snapshot-probe.v1", status="completed_limited_scope",
            second_process=args.delayed, startup=startup, startup_hex=startup_bytes.hex(), records=records,
            source_sha256=sources, engine_sha256=settings["engine_sha256"],
            fixed_hog_usage_proven=False, hidden_random_coverage_closed=False,
            training_admissible=False, gates_closed=False, seconds=time.monotonic() - started))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(error=repr(error), records=records))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
