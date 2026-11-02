"""Compare CPU and MPS on one real native FAIR Hog opening."""

import argparse
import json
import statistics
import time

import torch

from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import (MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK,
    tournament_level_kwargs)
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.sspi.learning import create_optimizer
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.protocol import ACTION_SCHEMA, SSPIAction
from native_runner.training.sspi.rollout import make_sspi_rollout
from native_runner.training.v4.factory import production_semantic_bundle


def probe_refill_decision(port: int, *, seed: int, host: str = "127.0.0.1",
        event_json=False):
    """Verify delayed Hog refill and ignored early PLAY on one native worker."""
    bundle = production_semantic_bundle()
    if event_json:
        from native_runner.training.sspi.collector import EventBatchedSSPINative
        native = EventBatchedSSPINative(host, port, timeout=90.)
    else:
        native = NativeClashEnv(host, port, timeout=90.)
    environment = FinalBattleEnv(native=native, card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog))
    try:
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=seed,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        if event_json:
            native.start_atomic_steps(initial_tick=environment.current_tick)
        rollout = make_sspi_rollout(environment, catalog=bundle.card_catalog, execution_seed=seed + 1)
        plays = ignored = pending_decisions = 0
        predicted = {}
        arrivals = []
        for _ in range(120):
            frozen = rollout.freeze_due_information()
            if frozen:
                actions = {owner: FullAction() for owner in frozen}
                if 0 in frozen and plays < 2:
                    info = frozen[0]
                    player = next(item for item in environment.raw_observation["players"]
                        if item["owner"] == 0)
                    costs = {int(card["cardId"]): int(card["cost"]) for card in player["hand"]}
                    choices = []
                    for row, card_id in enumerate(info.legality.card_ids):
                        if card_id not in costs:
                            continue
                        cells = torch.nonzero(info.legality.plays[row], as_tuple=False)
                        if cells.numel():
                            cell, delay = (int(value) for value in cells[0])
                            choices.append((costs[card_id], card_id, cell, delay))
                    if choices:
                        _, card_id, cell, delay = min(choices)
                        actions[0] = FullAction(card_id, cell, (2, 4, 6, 8, 10)[delay])
                        plays += 1
                elif 0 in frozen and frozen[0].hand.pending_refills and not ignored:
                    info = frozen[0]
                    refill = info.hand.pending_refills[0]
                    row = info.legality.card_ids.index(refill.card_id)
                    cells = torch.nonzero(info.legality.plays[row, :, 0], as_tuple=False)
                    if cells.numel() and refill.remaining_ticks >= 4:
                        actions[0] = FullAction(refill.card_id, int(cells[0, 0]), 2)
                        ignored += 1
                if 0 in frozen and frozen[0].hand.pending_refills:
                    pending_decisions += 1
                    for refill in frozen[0].hand.pending_refills:
                        if refill.time_known:
                            predicted[refill.card_id] = frozen[0].tick + refill.remaining_ticks
                rollout.commit_simultaneous(actions)
                if ignored and 0 in frozen and actions[0].card_id in predicted:
                    if rollout.runtime.clock.pending[0].action.card_id is not None:
                        raise ValueError("early queued-card proposal reached native execution")
            before = {player["owner"]: {card["cardId"] for card in player["hand"]}
                for player in environment.raw_observation["players"]}
            rollout.advance()
            after = {player["owner"]: {card["cardId"] for card in player["hand"]}
                for player in environment.raw_observation["players"]}
            arrived = after[0] - before[0]
            if arrived and len(before[0]) < 4:
                arrivals.append(dict(tick=rollout.runtime.clock.tick, cards=sorted(arrived),
                    predicted_ticks={str(card): predicted.get(card) for card in sorted(arrived)},
                    woke_owner0=0 in rollout.runtime.clock.ready))
                break
        if plays != 2 or ignored != 1 or not pending_decisions or not arrivals:
            raise ValueError("bounded native Hog trajectory did not expose delayed refill")
        if any(entry["predicted_ticks"].get(str(card)) != entry["tick"]
                for entry in arrivals for card in entry["cards"]):
            raise ValueError(f"predicted Hog refill tick differs from native arrival: {arrivals}")
        if any(entry["woke_owner0"] for entry in arrivals):
            raise ValueError("native refill unexpectedly woke the SSPI decision clock")
        return dict(status="passed", port=port, engine_sha256=native.attest().libg_sha256,
            plays=plays, pending_decisions=pending_decisions, ignored_early_plays=ignored,
            arrivals=arrivals, wake_refills=False, event_json=event_json)
    finally:
        native.close_transport()


def benchmark_step(model, information, *, device):
    optimizer = create_optimizer(model)
    model.train()
    optimizer.zero_grad(set_to_none=True)
    started = time.perf_counter()
    policies, value_logits = model(information, [information.legality])
    loss = -policies[0].log_prob(SSPIAction.wait()) + value_logits.square().mean()
    loss.backward()
    gradient_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
    optimizer.step()
    if device == "mps":
        torch.mps.synchronize()
    seconds = time.perf_counter() - started
    if (not torch.isfinite(loss) or not torch.isfinite(gradient_norm)
            or float(gradient_norm.detach().cpu()) <= 0):
        raise ValueError(f"{device} SSPI forward/backward did not produce finite gradients")
    return dict(seconds=seconds, loss=float(loss.detach().cpu()),
        gradient_norm=float(gradient_norm.detach().cpu()), optimizer_steps=1)


def run(port: int, *, seed: int, host: str = "127.0.0.1"):
    if not 1 <= port <= 65535 or type(seed) is not int or seed < 0:
        raise ValueError("invalid native port or model seed")
    if not torch.backends.mps.is_built() or not torch.backends.mps.is_available():
        raise RuntimeError("PyTorch MPS is not available in this host interpreter")
    bundle = production_semantic_bundle()
    native = NativeClashEnv(host, port, timeout=90.)
    environment = FinalBattleEnv(native=native, card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog))
    rollout = None
    try:
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=seed,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        rollout = make_sspi_rollout(environment, catalog=bundle.card_catalog, execution_seed=seed + 1)
        information = rollout.freeze_due_information()[0]
        if information.hand.ready_hand_count != 4 or information.legality.ability_candidates:
            raise ValueError("native opening differs from the fixed Hog FAIR contract")

        base = initialize_fresh_model(seed, device="cpu", bundle=bundle)
        initial_state = {name: value.detach().cpu().clone()
            for name, value in base.state_dict().items()}

        def fresh_on(device):
            current = initialize_fresh_model(seed, device=device, bundle=bundle)
            if any(not torch.equal(value.detach().cpu(), initial_state[name])
                    for name, value in current.state_dict().items()):
                raise ValueError(f"{device} model initialization differs from the shared theta0")
            return current

        base.eval()
        outputs = {}
        for device in ("cpu", "mps"):
            current = fresh_on(device)
            current.eval()
            with torch.inference_mode():
                policies, values = current(information, [information.legality])
                outputs[device] = dict(wait_log_probability=float(policies[0].log_prob(
                    SSPIAction.wait()).detach().cpu()), wdl=values[0].softmax(-1).cpu().tolist())
            del current

        errors = dict(wait_log_probability=abs(outputs["cpu"]["wait_log_probability"]
            - outputs["mps"]["wait_log_probability"]),
            wdl_max_abs=max(abs(left-right) for left, right in zip(outputs["cpu"]["wdl"], outputs["mps"]["wdl"])))
        if errors["wdl_max_abs"] > 2e-3 or errors["wait_log_probability"] > 2e-3:
            raise ValueError(f"CPU/MPS inference differs beyond pilot tolerance: {errors}")

        timings = {}
        for device in ("cpu", "mps"):
            current = fresh_on(device)
            benchmark_step(current, information, device=device)
            samples = [benchmark_step(current, information, device=device)["seconds"]
                for _ in range(3)]
            timings[device] = dict(samples_seconds=samples,
                median_seconds=statistics.median(samples))
            del current
        return dict(status="passed", native_port=port, native_engine_sha256=native.attest().libg_sha256,
            fair_sha256=information.sha256, hand_count=information.hand.ready_hand_count,
            action_schema=ACTION_SCHEMA, delay_ms=[200, 400, 600, 800, 1000],
            wait_ticks=20, parameters=base.parameter_count, model_seed=seed, inference=outputs,
            max_abs_error=errors, forward_backward_optimizer=timings,
            backend_preference=min(timings, key=lambda item: timings[item]["median_seconds"]),
            objective="operator-and-gradient-smoke-only; synthetic WAIT/WDL probe, not training data")
    finally:
        if rollout is not None:
            rollout.runtime.frozen.clear()
            rollout.information.clear()
        native.close_transport()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--refill-only", action="store_true")
    parser.add_argument("--event-json", action="store_true")
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.manual_seed(args.seed)
    result = (probe_refill_decision(args.port, seed=args.seed, host=args.host,
        event_json=args.event_json)
        if args.refill_only else run(args.port, seed=args.seed, host=args.host))
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
