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
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.sspi.learning import create_optimizer
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.protocol import SSPIAction
from native_runner.training.sspi.rollout import make_sspi_rollout
from native_runner.training.v4.factory import production_semantic_bundle


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
            action_schema="sspi-tagged-action.v1", delay_ms=[200, 400, 600, 800, 1000],
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
    args = parser.parse_args()
    torch.set_num_threads(1)
    torch.manual_seed(args.seed)
    print(json.dumps(run(args.port, seed=args.seed, host=args.host), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
