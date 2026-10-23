import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

from combat_protocol import BRANCH_TICKS, EXECUTION_SEED, MATCH_SEED, THRESHOLDS, choose, mask_record
from probe_snapshot import SCOPE, SnapshotGlobalAudit
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.transport import create_search_native
from native_runner.training.v4.factory import production_semantic_bundle


def public_state(environment):
    observation = environment.observe_dual(policy_features_only=True)[0]
    return dict(towers=[dict(entity_id=item.entity_id, owner=item.owner, hp=item.hitpoints,
        max_hp=item.max_hitpoints, active=item.active) for item in observation.towers],
        entities=[dict(entity_id=item.entity_id, owner=item.owner, card_id=item.card_id,
            hp=item.hitpoints, max_hp=item.max_hitpoints, position=item.position) for item in observation.entities])


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "probe", "libg", "puct-root", "output", "project"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delayed", action="store_true")
    args = parser.parse_args()
    torch.set_num_threads(1)
    settings = json.loads((Path(__file__).parent / "settings.json").read_text())
    sources = json.loads((args.project / "probe-source-manifest.json").read_text())
    for name, expected in sources.items():
        if hashlib.sha256((args.project / name).read_bytes()).hexdigest() != expected:
            raise ValueError("combat frozen runtime differs")
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    native = create_search_native(args.port, transport="compact-delta")
    trajectory, records, started = [], [], time.monotonic()
    try:
        audit = SnapshotGlobalAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port,
            probe_sha=settings["engine_sha256"])
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=MATCH_SEED,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=production_semantic_bundle().card_catalog,
            execution_seed=EXECUTION_SEED)

        def state():
            information = rollout.freeze_due_information()
            digest = native._request("state-digest")
            if digest.get("scope") != SCOPE:
                raise ValueError("combat digest scope differs")
            _, census = audit.global_state(native.observe())
            return dict(tick=environment.current_tick,
                digest={name: digest[name] for name in ("tick", "scope", "sha256", "nativeSha256")},
                clock_sha=semantic_digest(rollout.runtime.clock.capture()),
                fair_sha={str(owner): info.sha256 for owner, info in information.items()},
                rng_sha={str(owner): semantic_digest(generator.get_state())
                    for owner, generator in rollout.generators.items()},
                global_sha=census["state"]["sha256"], public=public_state(environment),
                ready=sorted(rollout.runtime.clock.ready),
                next_identity=rollout.runtime.clock.next_identity,
                pending=[asdict(plan) for plan in rollout.runtime.clock.pending.values()])

        def advance():
            if time.monotonic() - started > 300:
                raise TimeoutError("bounded combat snapshot deadline")
            if rollout.runtime.failed or rollout.runtime.ended or rollout.runtime.truncated:
                raise ValueError("combat trajectory stopped before registered boundary")
            evidence_start = len(rollout.runtime.clock.evidence)
            information = rollout.freeze_due_information()
            actions = {owner: choose(info) for owner, info in information.items()}
            decision = {str(owner): dict(action=asdict(actions[owner]), mask=mask_record(info), fair_sha=info.sha256)
                for owner, info in information.items()}
            if information:
                rollout.commit_simultaneous(actions)
            committed = [asdict(rollout.runtime.clock.pending[owner]) for owner in sorted(information)]
            outcomes = rollout.advance()
            if any(reason != "executed" for reason in outcomes.values()):
                raise ValueError("combat legal reservation rejected")
            if rollout.runtime.failed or rollout.runtime.ended or rollout.runtime.truncated:
                raise ValueError("combat trajectory ended before bounded continuation")
            return dict(decision=decision, committed=committed, outcomes=outcomes, after=state(),
                evidence=rollout.runtime.clock.evidence[evidence_start:])

        opening = state()
        for threshold in THRESHOLDS:
            while environment.current_tick < threshold:
                trajectory.append(advance())
            root = state()
            original, census = audit.global_state(native.observe())
            with rollout.snapshot(BattleEnvSidecar) as snapshot:
                branches, restored_states, restored_censuses = [], [], []
                for _ in range(2):
                    branches.append([advance() for _ in range(BRANCH_TICKS)])
                    snapshot.restore()
                    restored = state()
                    restored_bytes, restored_census = audit.global_state(native.observe())
                    if restored != root or restored_bytes != original:
                        raise ValueError("combat root restoration differs")
                    restored_states.append(restored)
                    restored_censuses.append(restored_census)
                if branches[0] != branches[1]:
                    raise ValueError("combat snapshot repeated continuation differs")
            if snapshot.native.handle is not None or state() != root:
                raise ValueError("combat snapshot handle release or root differs")
            records.append(dict(tick=threshold, root=root, census=census, global_hex=original.hex(),
                branches=branches, restored=restored_states, restored_censuses=restored_censuses,
                snapshot_released=True))
            atomic_json(args.output.with_name("progress.json"), dict(records=records, trajectory_ticks=len(trajectory)))
            print(json.dumps(dict(tick=threshold, status="continued_twice", seconds=time.monotonic() - started)), flush=True)
        atomic_json(args.output, dict(schema="fair-final-combat-snapshot-probe.v1", status="completed_limited_scope",
            second_process=args.delayed, engine_sha256=settings["engine_sha256"], source_sha256=sources,
            match_seed=MATCH_SEED, execution_seed=EXECUTION_SEED, opening=opening,
            trajectory=trajectory, records=records, global_monitor_readonly=True,
            fixed_hog_usage_proven=False, hidden_random_coverage_closed=False,
            training_admissible=False, gates_closed=False, seconds=time.monotonic() - started))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(error=repr(error), records=records,
            trajectory_ticks=len(trajectory), seconds=time.monotonic() - started))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
