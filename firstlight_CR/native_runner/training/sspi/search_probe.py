"""Run one reproducible SSPI PUCT root against independent native Hog state."""

import argparse
import json
from pathlib import Path
import sys
import time

import torch

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "research" / "puct-runtime"))

from native_adapter import BattleEnvSidecar
from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.opening_belief import OpeningBeliefProvider
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits, split_seed
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
from native_runner.training.sspi.learning import SEARCH_BUDGETS
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.rollout import make_sspi_rollout
from native_runner.training.sspi.search import budget_search_sha, make_search_inference
from native_runner.training.v4.factory import production_semantic_bundle


class LegacySearchNative(NativeClashEnv):
    """Use the legacy worker, whose native snapshot commands are supported."""

    def start_atomic_steps(self):
        return None


class TrackedOpeningBeliefProvider(OpeningBeliefProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_world = None

    def build(self, information, *, seed):
        world = super().build(information, seed=seed)
        self.last_world = world
        return world


def run(root_port, belief_port, *, seed=20261101, simulations=1024):
    if (type(root_port) is not int or type(belief_port) is not int or root_port == belief_port
            or type(seed) is not int or seed < 0 or simulations not in SEARCH_BUDGETS.values()):
        raise ValueError("invalid independent ports, seed, or SSPI fixed search budget")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    bundle = production_semantic_bundle()
    actual_native = LegacySearchNative("127.0.0.1", root_port, timeout=90.)
    environment = FinalBattleEnv(native=actual_native, card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog))
    belief = None
    rollout = None
    try:
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK,
            seed=split_seed(seed, "actual-match") % (2**31 - 1),
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        actual_native.start_atomic_steps()
        rollout = make_sspi_rollout(environment, catalog=bundle.card_catalog,
            execution_seed=split_seed(seed, "actual-rollout"))
        information = rollout.freeze_due_information()[0]
        observation = rollout.runtime.frozen[0]
        recorder = FairHistoryRecorder(observation, information, opening_tick=environment.warmup_ticks)
        opening = recorder.opening
        if information.hand.ready_hand_count != 4 or information.legality.ability_candidates:
            raise ValueError("native opening differs from SSPI Hog legality")

        model = initialize_fresh_model(seed, device="cpu", bundle=bundle).eval()
        model_sha = semantic_digest(model.state_dict())
        controller_sha = semantic_digest(dict(model_sha=model_sha, action_schema="sspi-tagged-action.v1",
            delay_ms=[200, 400, 600, 800, 1000], wait_ticks=20, device="cpu"))
        inference = make_search_inference(model, checkpoint_sha=model_sha, controller_sha=controller_sha)
        belief = TrackedOpeningBeliefProvider(lambda: LegacySearchNative("127.0.0.1", belief_port, timeout=90.),
            evidence=opening, catalog=bundle.card_catalog, sidecar_factory=BattleEnvSidecar,
            rollout_factory=make_sspi_rollout)
        limits = SearchLimits(root_noise=.25, root_noise_mode="training")
        search_sha = budget_search_sha(limits, simulations)
        identity = dict(search_protocol_sha=search_sha, root_belief_prior_sha=belief.sampler_sha,
            checkpoint_blue_sha=model_sha, checkpoint_red_sha=model_sha,
            controller_blue_sha=controller_sha, controller_red_sha=controller_sha)
        before_native = semantic_digest(environment.raw_observation)
        started = time.perf_counter()
        outcome = CompletedBoundarySearch(belief, inference, inference, limits=limits).run(
            information, seed=split_seed(seed, "search"), behavior_seed=split_seed(seed, "behavior"),
            codec=ActionCodec(HOG_DECK), identity=identity, mode="FULL", simulation_budget=simulations)
        elapsed = time.perf_counter() - started
        after_native = semantic_digest(environment.raw_observation)
        after_information = rollout.freeze_due_information()[0]
        if (before_native != after_native or information.sha256 != after_information.sha256
                or belief.last_world is None or not belief.last_world.closed
                or outcome.result["valid_simulations"] != simulations
                or sum(row["visits"] for row in outcome.result["candidates"]) != simulations):
            raise ValueError("native search failed exact visits, root restore, or lease release checks")
        return dict(status="passed", mode="SEARCH", simulations=simulations,
            valid_simulations=outcome.result["valid_simulations"], attempts=outcome.audit["attempts"],
            native_ticks=outcome.audit["timings"], seconds=elapsed, audit=outcome.audit,
            selected_action=outcome.selected.__dict__, owner=information.owner, tick=information.tick,
            fair_sha256=information.sha256, native_root_digest_unchanged=True,
            independent_belief_lease_released=belief.last_world.closed,
            engine_sha256=actual_native.attest().libg_sha256, model_sha256=model_sha,
            controller_sha256=controller_sha, search_sha256=search_sha,
            belief_sampler_sha256=belief.sampler_sha, parameters=model.parameter_count,
            seed=seed, devices={"policy": "cpu", "engine": "android-arm64-avd"})
    finally:
        if rollout is not None:
            rollout.runtime.frozen.clear()
            rollout.information.clear()
        actual_native.close_transport()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--simulations", type=int, choices=(128, 1024), default=1024)
    args = parser.parse_args()
    print(json.dumps(run(args.root_port, args.belief_port, seed=args.seed,
        simulations=args.simulations), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
