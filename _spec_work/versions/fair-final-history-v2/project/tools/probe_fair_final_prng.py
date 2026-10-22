import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.history_belief import HistoryBeliefFactory
from native_runner.training.fair_final.history_provider import HistorySearchProvider
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.fair_final_prng_audit import NativePrngAudit
from tools.probe_fair_final_extended_search import BoundaryAuditProvider, SOURCE_SHA
from tools.probe_fair_final_visibility import policy_signature, refreshed_information


def perturb_prng(rollout, audit):
    native = rollout.runtime.environment.native
    raw = native.observe()
    before_digest = native._request("state-digest")["sha256"]
    before = audit.inspect(raw)
    mutation = audit.inspect(raw, advance=True, expected=before["before"])
    after_digest = native._request("state-digest")["sha256"]
    after_raw = native.observe()
    if (raw != after_raw or before_digest == after_digest or mutation["changed"] is not True
            or audit.inspect(after_raw)["before"] != mutation["after"]):
        raise ValueError("PRNG mutation changed observation or failed native readback")
    return dict(**mutation, before_digest=before_digest, after_digest=after_digest, raw_unchanged=True)


def validate_boundary(rollout, source, audit, sidecar):
    native = rollout.runtime.environment.native
    original = native._request("state-digest")["sha256"]
    with rollout.snapshot(sidecar) as snapshot:
        before = {owner: policy_signature(source, info)
            for owner, info in refreshed_information(rollout).items()}
        mutation = perturb_prng(rollout, audit)
        after = {owner: policy_signature(source, info)
            for owner, info in refreshed_information(rollout).items()}
        if len(before) != 2 or before != after:
            raise ValueError("native PRNG changed FAIR view, policy, candidates or value")
        stale_rejected = []
        raw = native.observe()
        for key in ("generation", "stateEpoch", "tick"):
            try:
                audit.inspect({**raw, key: raw[key] + 1}, advance=True)
            except ValueError as error:
                if "stale or active" not in str(error):
                    raise
                stale_rejected.append(key)
            else:
                raise ValueError("stale PRNG request was accepted")
        try:
            audit.inspect(raw, advance=True, expected=mutation["before"])
        except ValueError as error:
            if "compare-and-swap" not in str(error):
                raise
            stale_rejected.append("expected_state")
        else:
            raise ValueError("stale PRNG state was accepted")
        if audit.inspect(raw)["before"] != mutation["after"]:
            raise ValueError("rejected PRNG requests changed state")
        snapshot.restore()
        restored = audit.inspect(native.observe())["before"]
        if restored != mutation["before"] or native._request("state-digest")["sha256"] != original:
            raise ValueError("snapshot did not restore the native PRNG")
    if snapshot.native.handle is not None:
        raise ValueError("PRNG diagnostic snapshot leaked")
    return dict(mutation=mutation, policy_before=before, policy_after=after,
        restored_state=restored, restored_digest=original, stale_rejected=stale_rejected, snapshot_released=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--libg", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=int, choices=(100, 1200, 3600, 5400), required=True)
    parser.add_argument("--owner", type=int, choices=(0, 1), required=True)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    if args.port == args.belief_port:
        raise ValueError("actual and belief engines must be independent")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic() - started >= 3540:
            raise TimeoutError("bounded PRNG acceptance deadline")

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("PRNG probe source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    limits = SearchLimits()
    native = create_search_native(args.port, transport="compact-delta")
    records = []
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101961,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101971)
        initial = rollout.freeze_due_information()
        recorders = {owner: FairHistoryRecorder(rollout.runtime.frozen[owner], info,
            opening_tick=environment.warmup_ticks) for owner, info in initial.items()}
        while environment.current_tick < args.threshold or rollout.runtime.clock.ready != {0, 1}:
            deadline()
            if rollout.runtime.ended or rollout.runtime.failed or rollout.runtime.truncated:
                raise ValueError("actual trajectory ended before registered PRNG stage")
            frozen = rollout.freeze_due_information()
            if frozen:
                actions = {owner: inference.sample(info, rollout.generators[owner]) for owner, info in frozen.items()}
                rollout.commit_simultaneous(actions)
                for owner, info in frozen.items():
                    recorders[owner].record_decision(info, actions[owner])
            if any(reason != "executed" for reason in rollout.advance().values()):
                raise ValueError("actual trajectory contains rejected reservation")
            observations = environment.observe_dual(policy_features_only=True)
            for owner in (0, 1):
                recorders[owner].observe_tick(observations[owner])
        audit = NativePrngAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port)
        boundary = validate_boundary(rollout, source, audit, BattleEnvSidecar)
        atomic_json(args.output.with_name("boundary.json"), boundary)
        print(json.dumps(dict(kind="prng_boundary_passed", tick=environment.current_tick,
            owner=args.owner, mutation=boundary["mutation"])), flush=True)
        factory = HistoryBeliefFactory(lambda: create_search_native(args.belief_port, transport="compact-delta"),
            catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
        original = native._request("state-digest")["sha256"]
        with rollout.snapshot(BattleEnvSidecar) as actual_root:
            for altered in (() if args.smoke_only else (False, True)):
                actual_root.restore()
                mutation = perturb_prng(rollout, audit) if altered else None
                information = refreshed_information(rollout)[args.owner]
                history = recorders[args.owner].capture(information)
                atomic_json(args.output.with_name(f"history-{int(altered)}.json"), history.to_payload())
                provider = HistorySearchProvider(factory, history=history, check_deadline=deadline)
                audited = BoundaryAuditProvider(provider)
                identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                    checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                    controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
                before = native._request("state-digest")["sha256"]
                state_before = audit.inspect(native.observe())["before"]
                print(json.dumps(dict(kind="search_started", altered=altered, tick=information.tick)), flush=True)
                try:
                    search = CompletedBoundarySearch(audited, inference, inference, limits=limits)
                    outcome = search.run(information, seed=78102103, behavior_seed=78102104,
                        codec=ActionCodec(HOG_DECK), identity=identity, deadline=started+3540)
                    if provider.active is not None:
                        raise ValueError("PRNG search world lease leaked")
                finally:
                    provider.close()
                after = native._request("state-digest")["sha256"]
                state_after = audit.inspect(native.observe())["before"]
                if before != after or state_before != state_after:
                    raise ValueError("PRNG search modified actual native state")
                records.append(dict(owner=args.owner, altered=altered, tick=information.tick,
                    fair_sha=information.sha256, history_sha=history.sha256,
                    outcome=dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target),
                    audit=outcome.audit, attempts=audited.attempts, mutation=mutation,
                    actual_digest_before=before, actual_digest_after=after,
                    actual_prng_before=state_before, actual_prng_after=state_after, lease_released=True))
                atomic_json(args.output.with_name("progress.json"), dict(records=records))
                print(json.dumps(dict(kind="search_completed", altered=altered,
                    simulations=outcome.result["valid_simulations"], seconds=outcome.audit["seconds"])), flush=True)
            if not args.smoke_only:
                for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts"):
                    if records[0][key] != records[1][key]:
                        raise ValueError(f"native PRNG changed full-horizon search {key}")
            actual_root.restore()
        if (native._request("state-digest")["sha256"] != original or actual_root.native.handle is not None
                or audit.inspect(native.observe())["before"] != boundary["restored_state"]):
            raise ValueError("actual PRNG root restoration or snapshot release failed")
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("PRNG test modified frozen model")
        atomic_json(args.output, dict(schema="fair-final-native-prng.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            threshold=args.threshold, owner=args.owner, smoke_only=args.smoke_only, limits=asdict(limits),
            records=records, boundary=boundary, model_unchanged=True, actual_root_restored=True,
            snapshot_released=True, seconds=time.monotonic()-started,
            exclusions=["other native PRNG instances", "general empirical prior",
                "constant-cost reconditioning", "full-episode search quality"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error),
            records=records, seconds=time.monotonic()-started, source_sha256=manifest))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
