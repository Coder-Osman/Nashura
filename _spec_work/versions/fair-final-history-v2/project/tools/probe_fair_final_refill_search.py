import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.prior_provider import PriorHistorySearchProvider
from native_runner.training.fair_final.refill_belief import RefillPriorHistoryBeliefFactory, native_draw_layout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.probe_fair_final_prior_search import PriorAuditProvider, SOURCE_SHA


class RefillAuditProvider(PriorAuditProvider):
    def build(self, information, *, seed):
        world = super().build(information, seed=seed)
        try:
            self.draw_layouts = [dict(owner=player["owner"], **asdict(native_draw_layout(player)))
                for player in world.rollout.runtime.environment.raw_observation["players"]]
        except BaseException:
            world.close()
            raise
        return world


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    for name in ("source", "prior", "history", "puct-root", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic()-started > 3540:
            raise TimeoutError("bounded refill Full128 deadline")

    project = Path(__file__).resolve().parents[1]
    manifest = json.loads((project/"probe-source-manifest.json").read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((project/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("refill search source changed")
    history = FairHistory.from_payload(json.loads(args.history.read_text()))
    reference = SimpleNamespace(owner=history.opening.owner, tick=history.root_tick, sha256=history.root_fair_sha)
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_sha = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)),
        source_adapter=True)
    factory = RefillPriorHistoryBeliefFactory(lambda: create_search_native(args.port, transport="compact-delta"),
        prior=prior, catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    session = None
    provider = None
    try:
        session = factory.build(history, reference, seed=78102401, check_deadline=deadline)
        information = session.rollout.freeze_due_information()[reference.owner]
        native = session.rollout.runtime.environment.native
        layouts = [dict(owner=player["owner"], **asdict(native_draw_layout(player)))
            for player in session.rollout.runtime.environment.raw_observation["players"]]
        digest = native._request("state-digest")["sha256"]
        continued = []
        for repeat in range(2):
            session.root.restore()
            from native_runner.training.fair_final.policy import FullAction
            for offset in range(30):
                due = session.rollout.freeze_due_information()
                if due:
                    session.rollout.commit_simultaneous({owner: FullAction() for owner in due})
                session.rollout.advance()
            continued.append(dict(digest=native._request("state-digest")["sha256"],
                layouts=[asdict(native_draw_layout(player))
                    for player in session.rollout.runtime.environment.raw_observation["players"]]))
        session.root.restore()
        if continued[0] != continued[1] or native._request("state-digest")["sha256"] != digest:
            raise ValueError("independent refill continuation or root restore differs")
        reconstruction = dict(layouts=layouts, manifest=session.conditioning_manifest,
            continuation=continued, profile=session.profile, root_digest=digest)
        session.close()
        native.close_transport()
        session = None
        atomic_json(args.output.with_name("reconstruction.json"), reconstruction)
        print(json.dumps(dict(phase="refill-reconstructed", owner=reference.owner,
            tick=reference.tick, seconds=time.monotonic()-started)), flush=True)
        provider = PriorHistorySearchProvider(factory, history=history, check_deadline=deadline)
        audited = RefillAuditProvider(provider)
        limits = SearchLimits()
        identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
            checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
            controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
        outcome = CompletedBoundarySearch(audited, inference, inference, limits=limits).run(information,
            seed=78102403, behavior_seed=78102404, codec=ActionCodec(HOG_DECK), identity=identity,
            deadline=started+3540)
        if provider.active is not None or semantic_digest(source.state_dict()) != model_sha:
            raise ValueError("refill search leaked world or changed model")
        provider.close()
        atomic_json(args.output, dict(schema="fair-final-refill-search.v1", status="passed",
            training_admissible=False, source_sha256=manifest, checkpoint_sha256=source_sha,
            history_sha256=hashlib.sha256(args.history.read_bytes()).hexdigest(),
            history_sha=history.sha256, owner=reference.owner, tick=reference.tick, limits=asdict(limits),
            reconstruction=reconstruction, search_layouts=audited.draw_layouts,
            outcome=dict(result=outcome.result, selected=outcome.selected.key, target=outcome.policy_target),
            audit=outcome.audit, attempts=audited.attempts, world_lifecycle=audited.lifecycle,
            model_unchanged=True, seconds=time.monotonic()-started,
            exclusions=["local-refill-reconditioning", "actual-hidden-state-intervention",
                "general-pending-plan", "complete-game-search", "training-admission"]))
        print(json.dumps(dict(status="passed", tick=reference.tick, owner=reference.owner,
            simulations=outcome.result["valid_simulations"])), flush=True)
    except BaseException as error:
        atomic_json(args.output.with_name("failure.json"), dict(status="failed", error=repr(error)))
        raise
    finally:
        if session is not None:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
        if provider is not None:
            provider.close()
        factory.close()


if __name__ == "__main__":
    main()
