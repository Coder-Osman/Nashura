import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.refill_local import IncrementalRefillBeliefFactory, RefillDrawReconditioner
from native_runner.training.fair_final.refill_belief import native_draw_layout
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    for name in ("source", "prior", "history", "next-history", "puct-root", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic()-started > 1200:
            raise TimeoutError("bounded refill local probe deadline")

    project = Path(__file__).resolve().parents[1]
    sources = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in sources.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("refill local deployed source differs")
    histories = [FairHistory.from_payload(json.loads(path.read_text())) for path in (args.history, args.next_history)]
    if not histories[1].extends(histories[0]):
        raise ValueError("refill successor is not a verified FAIR extension")
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_sha = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    factory = IncrementalRefillBeliefFactory(lambda: create_search_native(args.port, transport="compact-delta"),
        prior=prior, catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    conditioner = RefillDrawReconditioner(factory)
    records, syncs, rollbacks = [], [], []
    session = None
    try:
        for index, history in enumerate(histories):
            reference = SimpleNamespace(owner=history.opening.owner, tick=history.root_tick, sha256=history.root_fair_sha)
            sync_started = time.monotonic()
            if session is None:
                session = factory.build(history, reference, seed=78102401, check_deadline=deadline)
            else:
                session.sync(history, reference, check_deadline=deadline)
            syncs.append(dict(mode="cold" if index == 0 else "incremental", seconds=time.monotonic()-sync_started,
                profile=deepcopy(session.profile), history_sha=history.sha256,
                construction_manifest=deepcopy(session.conditioning_manifest)))
            print(json.dumps(dict(phase="refill-local-sync", tick=history.root_tick, sync=syncs[-1])), flush=True)
            rollout = session.rollout
            native = rollout.runtime.environment.native
            information = rollout.freeze_due_information()[reference.owner]
            anchor_digest = native._request("state-digest")["sha256"]
            anchor_layouts = [asdict(native_draw_layout(player)) for player in native.observe()["players"]]
            anchor_completion = asdict(session.draw_completion)
            signatures = {}
            def continuation():
                result = []
                for offset in range(30):
                    due = rollout.freeze_due_information()
                    if due:
                        rollout.commit_simultaneous({owner: FullAction() for owner in due})
                    rollout.advance()
                    if rollout.runtime.failed or rollout.runtime.ended or rollout.runtime.truncated:
                        raise ValueError("local refill continuation terminated unexpectedly")
                    result.append(dict(tick=rollout.runtime.clock.tick,
                        layouts=[asdict(native_draw_layout(player)) for player in native.observe()["players"]]))
                return dict(ticks=result, digest=native._request("state-digest")["sha256"])
            with rollout.snapshot(BattleEnvSidecar) as baseline_snapshot:
                baseline = continuation()
                baseline_snapshot.restore()
            for seed in (78102501, 78102502, 78102501):
                session.root.restore()
                result = conditioner.apply(session, history, information, seed=seed, check_deadline=deadline)
                local_digest = native._request("state-digest")["sha256"]
                local_layouts = [asdict(native_draw_layout(player)) for player in native.observe()["players"]]
                continued = []
                with rollout.snapshot(BattleEnvSidecar) as local_snapshot:
                    for repeat in range(2):
                        local_snapshot.restore()
                        continued.append(continuation())
                    local_snapshot.restore()
                if continued[0] != continued[1] or native._request("state-digest")["sha256"] != local_digest:
                    raise ValueError("local refill continuation or restore differs")
                for before, after in zip(baseline["ticks"], continued[0]["ticks"]):
                    if before["tick"] != after["tick"]:
                        raise ValueError("local refill changed simulation time")
                    for old, new in zip(before["layouts"], after["layouts"]):
                        if ([card is None for card in old["hand"]] != [card is None for card in new["hand"]]
                                or len(old["cycle"]) != len(new["cycle"])):
                            raise ValueError("local relabeling changed refill timing or hole positions")
                signature = dict(manifest=result["manifest"], digest=local_digest, continuation=continued[0])
                if seed in signatures and signatures[seed] != signature:
                    raise ValueError("local refill seed depends on preceding completion")
                signatures[seed] = signature
                session.root.restore()
                if native._request("state-digest")["sha256"] != anchor_digest:
                    raise ValueError("local refill corrupted its independent anchor")
                records.append(dict(tick=history.root_tick, owner=reference.owner, seed=seed,
                    seconds=result["seconds"], manifest=result["manifest"], completion=asdict(result["completion"]),
                    anchor_digest=anchor_digest, local_digest=local_digest, anchor_layouts=anchor_layouts,
                    anchor_completion=anchor_completion, baseline=baseline, local_layouts=local_layouts,
                    continuations=continued, anchor_restored=True, refill_timing_unchanged=True))
                atomic_json(args.output.with_name("progress.json"), dict(records=records, syncs=syncs))
            session.root.restore()
            calls = 0
            def fail_after_mutation():
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise TimeoutError("injected post-mutation failure")
            try:
                conditioner.apply(session, history, information, seed=78102503, check_deadline=fail_after_mutation)
            except TimeoutError:
                restored_digest = native._request("state-digest")["sha256"]
                if calls != 3 or restored_digest != anchor_digest:
                    raise ValueError("failed local refill mutation did not restore its anchor")
                rollbacks.append(dict(tick=history.root_tick, owner=reference.owner, calls=calls,
                    anchor_digest=anchor_digest, restored_digest=restored_digest))
            else:
                raise ValueError("injected local failure did not occur")
        if semantic_digest(source.state_dict()) != model_sha:
            raise ValueError("local refill mutated frozen model")
        session.close()
        native.close_transport()
        session = None
        atomic_json(args.output, dict(schema="fair-final-refill-local-native.v1", status="passed",
            training_admissible=False, source_sha256=sources, checkpoint_sha256=source_sha,
            sampler_sha=conditioner.sampler_sha, prior_sha=prior.sha256, records=records, syncs=syncs,
            rollbacks=rollbacks,
            model_unchanged=True, post_mutation_rollback_verified=True, seconds=time.monotonic()-started,
            exclusions=["full-posterior-resampling", "general-pending-plan", "Full128-local-search",
                "complete-game-search", "throughput-ab", "training-admission"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error), records=records, syncs=syncs))
        raise
    finally:
        if session is not None:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
        factory.close()


if __name__ == "__main__":
    main()
