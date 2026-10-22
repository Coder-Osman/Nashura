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
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.opening_belief import OpeningBeliefProvider, OpeningEvidence
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from probe_fair_final_visibility import perturb_draw, refreshed_information


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.port == args.belief_port:
        raise ValueError("actual and belief engines must have independent processes")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("search probe deployed source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256="e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015")
    source.eval()
    model_before = semantic_digest(source.state_dict())
    controller = semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.))
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha, controller_sha=controller, source_adapter=True)
    limits = SearchLimits(soft_decisions=2, soft_ticks=60)
    native = create_search_native(args.port, transport="compact-delta")
    records = []
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78102001,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78102002)
        with rollout.snapshot(BattleEnvSidecar) as initial, torch.inference_mode():
            for owner in (0, 1):
                outcomes, views, mutations = [], [], []
                for altered in (False, True):
                    initial.restore()
                    if altered:
                        mutations.append(perturb_draw(rollout, owner))
                    information = refreshed_information(rollout)[owner]
                    evidence = OpeningEvidence.from_view(rollout.runtime.frozen[owner], information,
                        opening_tick=environment.warmup_ticks)
                    belief = OpeningBeliefProvider(lambda: create_search_native(args.belief_port, transport="compact-delta"),
                        evidence=evidence, catalog=source.policy.backbone.catalog, sidecar_factory=BattleEnvSidecar)
                    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=belief.sampler_sha,
                        checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                        controller_blue_sha=controller, controller_red_sha=controller)
                    digest_before = native._request("state-digest")["sha256"]
                    search = CompletedBoundarySearch(belief, inference, inference, limits=limits)
                    result = search.run(information, seed=78102003, behavior_seed=78102004,
                        identity=identity, codec=ActionCodec(HOG_DECK), deadline=started+1740)
                    if native._request("state-digest")["sha256"] != digest_before:
                        raise ValueError("search advanced or modified the actual match")
                    outcomes.append(dict(result=result.result, selected=result.selected.key, target=result.policy_target))
                    views.append(information.sha256)
                    record = dict(owner=owner, altered=altered, outcome=outcomes[-1], audit=result.audit)
                    records.append(record)
                    atomic_json(args.output.with_name("search-progress.json"), dict(completed_roots=len(records),
                        owner=owner, altered=altered, seconds=time.monotonic()-started))
                    print(json.dumps(dict(owner=owner, altered=altered, visits=result.result["valid_simulations"],
                        seconds=result.audit["seconds"], invalid=result.result["invalid_attempts"],
                        lifecycle=result.audit["completed_lifecycle"])), flush=True)
                if outcomes[0] != outcomes[1] or views[0] != views[1]:
                    raise ValueError("actual hidden draw changed independent-world Full128 search")
                records[-1]["noninterference"] = dict(exact=True, native_mutation=mutations[0])
            initial.restore()
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("frozen search model parameters or buffers changed")
        report = dict(schema="fair-final-native-search-acceptance.v1", status="passed",
            training_admissible=False, checkpoint_sha256=source_sha, source_sha256=manifest,
            limits=asdict(limits), search_protocol_sha=limits.sha256, records=records,
            seconds=time.monotonic()-started, model_unchanged=True, actual_match_unchanged=True,
            exclusions=["middle/late conditioned history reconstruction", "general empirical opponent deck prior",
                "native PRNG mutation", "full-episode search quality", "default 6-decision/200-tick horizon"])
        atomic_json(args.output, report)
        print(json.dumps(dict(status="passed", roots=len(records), seconds=report["seconds"])), flush=True)
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
