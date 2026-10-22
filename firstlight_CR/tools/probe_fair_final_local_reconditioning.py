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
from native_runner.training.fair_final.local_reconditioning import SettledDrawReconditioner
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.prior_history import PriorHistoryBeliefFactory
from native_runner.training.fair_final.search_runtime import FrozenSearchInference, RolloutSearchWorld
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic()-started > 1500:
            raise TimeoutError("bounded local conditioning deadline")

    project = Path(__file__).resolve().parents[1]
    identities = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in identities.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("local conditioning probe source changed")
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    source, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    before_model = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a")), source_adapter=True)
    factory = PriorHistoryBeliefFactory(lambda:create_search_native(args.belief_port, transport="compact-delta"),
        prior=prior, catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    conditioner = SettledDrawReconditioner(factory)
    records = []
    session = world = None
    try:
        for threshold in (1200, 3600):
            for owner in (0, 1):
                path = args.output.parent/f"history-{threshold}-{owner}.json"
                history = FairHistory.from_payload(json.loads(path.read_text()))
                reference = SimpleNamespace(owner=owner, tick=history.root_tick, sha256=history.root_fair_sha)
                session = factory.build(history, reference, seed=78102031+owner, check_deadline=deadline)
                information = session.rollout.freeze_due_information()[owner]
                native = session.rollout.runtime.environment.native
                anchor_digest = native._request("state-digest")["sha256"]
                cold_profile = deepcopy(session.profile)
                environment = session.rollout.runtime.environment
                anchor_phases = {item.native_object_id: asdict(item.phase_runtime)
                    for item in environment._rich_snapshot_for(environment.raw_observation).objects
                    if item is not None and item.phase_runtime is not None}
                signatures = {}
                for seed in (78102201+owner, 78102202+owner, 78102201+owner):
                    session.root.restore()
                    restored_phases = {item.native_object_id: asdict(item.phase_runtime)
                        for item in environment._rich_snapshot_for(environment.raw_observation).objects
                        if item is not None and item.phase_runtime is not None}
                    phase_differences = {str(identity): {name: dict(before=value,
                        after=restored_phases.get(identity, {}).get(name)) for name, value in phase.items()
                        if value != restored_phases.get(identity, {}).get(name)}
                        for identity, phase in anchor_phases.items() if phase != restored_phases.get(identity)}
                    print(json.dumps(dict(phase="anchor-restore-telemetry", threshold=threshold,
                        owner=owner, seed=seed, differences=phase_differences)), flush=True)
                    if anchor_phases != restored_phases:
                        raise ValueError("root restore changed native phase state or entity coverage")
                    result = conditioner.apply(session, history, information, seed=seed, check_deadline=deadline)
                    local_digest = native._request("state-digest")["sha256"]
                    world = RolloutSearchWorld(session.rollout, owner=owner,
                        sidecar_factory=BattleEnvSidecar, manifest=result["manifest"])
                    continuations = []
                    for repeat in range(2):
                        world.restore_root()
                        transition = world.transition(FullAction(), opponent=inference,
                            generator=torch.Generator().manual_seed(78102119), hard_tick=information.tick+120,
                            maximum_decisions=6, check_deadline=deadline)
                        if not transition.completed:
                            raise ValueError("local conditioned continuation did not finish its edge")
                        continuations.append(dict(completed=True, ticks=transition.ticks, decisions=transition.decisions,
                            reason=transition.reason, terminal_value=transition.terminal_value,
                            fair_sha=None if transition.information is None else transition.information.sha256,
                            native_digest=native._request("state-digest")["sha256"]))
                    world.close()
                    world = None
                    if continuations[0] != continuations[1] or native._request("state-digest")["sha256"] != local_digest:
                        raise ValueError("local continuation or snapshot restore changed")
                    signature = dict(manifest=result["manifest"], digest=local_digest, continuation=continuations[0])
                    if seed in signatures and signatures[seed] != signature:
                        raise ValueError("same local seed depends on preceding completion")
                    signatures[seed] = signature
                    session.root.restore()
                    if native._request("state-digest")["sha256"] != anchor_digest:
                        raise ValueError("local conditioning corrupted its independent anchor")
                    row = dict(threshold=threshold, owner=owner, tick=history.root_tick, seed=seed,
                        history_sha=history.sha256, history_file_sha=hashlib.sha256(path.read_bytes()).hexdigest(),
                        anchor_digest=anchor_digest, local_digest=local_digest, manifest=result["manifest"],
                        completion=asdict(result["completion"]), seconds=result["seconds"], cold_profile=cold_profile,
                        cycle_delta=result["cycle_delta"], continuations=continuations, anchor_restored=True,
                        native_phase_restored=True, native_phase_object_count=len(anchor_phases))
                    records.append(row)
                    atomic_json(args.output.with_name("progress.json"), dict(records=records))
                    print(json.dumps(dict(phase="local-root", threshold=threshold, owner=owner,
                        seed=seed, seconds=result["seconds"], total_seconds=time.monotonic()-started)), flush=True)
                session.close()
                native.close_transport()
                session = None
        if semantic_digest(source.state_dict()) != before_model:
            raise ValueError("local conditioning mutated frozen model")
        atomic_json(args.output, dict(schema="fair-final-native-local-reconditioning.v1", status="passed",
            training_admissible=False, source_sha256=identities, checkpoint_sha256=source_sha,
            prior_sha=prior.sha256, sampler_sha=conditioner.sampler_sha, records=records,
            model_unchanged=True, seconds=time.monotonic()-started,
            exclusions=["Full128-search", "actual-hidden-state-intervention", "single-ready-pending-plan",
                "incomplete-refills", "end-to-end-throughput", "training-admission"]))
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error), records=records))
        raise
    finally:
        if world is not None:
            world.close()
        if session is not None:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
        factory.close()


if __name__ == "__main__":
    main()
