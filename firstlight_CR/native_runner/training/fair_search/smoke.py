import argparse
import hashlib
import importlib
import json
from pathlib import Path
import time

import torch

from .belief import IncrementalBeliefProvider, ReplayBeliefProvider
from .collector import collect_episode
from .config import SearchConfig, load_search_config
from .contracts import INFORMATION_DIGEST_SCHEMA, InformationState, semantic_digest
from .inference import LocalInference, SocketInferenceClient
from .initialize import load_selfplay_checkpoint
from .native import NativeMatchRuntime, NativeReplayFactory, install_puct
from .search import InformationSetSearch


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one complete FAIR self-play episode.")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--main-port", type=int, required=True)
    parser.add_argument("--belief-port", type=int, action="append", required=True)
    parser.add_argument("--opponent-belief-port", type=int, action="append", default=[])
    parser.add_argument("--belief-mode", choices=("incremental", "replay"), default="incremental")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1755)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--episode-id", default=None)
    parser.add_argument("--particles", type=int)
    parser.add_argument("--search-config", type=Path)
    parser.add_argument("--independent-visit-sampling", action="store_true")
    parser.add_argument("--belief-reserve-worlds", type=int, default=0)
    parser.add_argument("--belief-attempts", type=int, default=32)
    parser.add_argument("--conditional-draws", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=7200.)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--transport", choices=("compact", "compact-delta", "json"), default="compact")
    parser.add_argument("--replay-root", type=Path)
    parser.add_argument("--diagnostic-directory", type=Path)
    parser.add_argument("--search-root", action="store_true")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--inference-cache-entries", type=int, default=64)
    parser.add_argument("--inference-score-batch", type=int, default=32)
    parser.add_argument("--reference-sampling", action="store_true")
    parser.add_argument("--inference-socket", type=Path)
    parser.add_argument("--record-policy-steps", action="store_true",
                        help="Use the legacy local policy evaluation and PPO recorder for comparison.")
    args = parser.parse_args(argv)
    config = (load_search_config(args.search_config) if args.search_config else
              SearchConfig(particles=1 if args.particles is None else args.particles))
    if args.particles is not None and args.particles != config.particles:
        raise ValueError("particle override differs from explicit search configuration")
    args.particles = config.particles
    if args.threads < 1:
        raise ValueError("inference threads must be positive")
    if args.attempt < 0:
        raise ValueError("attempt must be nonnegative")
    if not 0 <= args.belief_reserve_worlds < 8:
        raise ValueError("belief reserve worlds must be between zero and seven")
    if args.belief_mode == "incremental" and args.particles + args.belief_reserve_worlds > 8:
        raise ValueError("active and reserve belief worlds must total at most eight")
    if args.search_root and args.replay_root is None:
        raise ValueError("search-root requires a saved FAIR replay-root")
    if args.inference_socket is not None and (args.reference_sampling
            or args.inference_cache_entries != 64 or args.inference_score_batch != 32):
        raise ValueError("local inference tuning flags do not configure an external service")
    torch.set_num_threads(args.threads)
    if args.main_port in args.belief_port:
        raise ValueError("main and belief ports must be exclusive")
    if args.replay_root is None and args.belief_mode == "incremental":
        ports = [args.main_port, *args.belief_port, *args.opponent_belief_port]
        required = args.particles + args.belief_reserve_worlds
        if (len(args.belief_port) != required or len(args.opponent_belief_port) != required
                or len(set(ports)) != len(ports)):
            raise ValueError("incremental belief requires exclusive active and reserve ports for each owner")
    elif args.particles != len(args.belief_port):
        raise ValueError("replay belief requires one exclusive port per particle")
    runtime_device = "cpu" if args.inference_socket is not None and not args.record_policy_steps else args.device
    model, checkpoint_sha256 = load_selfplay_checkpoint(
        args.checkpoint, initialization_path=args.output / "initial-checkpoint.pt",
        config=config, device=runtime_device)
    model.eval()
    rollout_type = install_puct(args.puct_root)
    runtime_sha256 = semantic_digest(dict(
        information_digest_schema=INFORMATION_DIGEST_SCHEMA,
        fair_contracts_sha256=_sha256(Path(__file__).with_name("contracts.py")),
        event_set_sha256=_sha256(Path(__file__).parents[1] / "event_set.py"),
        battle_env_sha256=_sha256(Path(__file__).parents[2] / "battle_env.py"),
        tracking_sha256=_sha256(Path(__file__).parents[1] / "tracking.py"),
        policy_rollout_sha256=_sha256(importlib.import_module("policy_rollout").__file__),
        state_codec_sha256=_sha256(importlib.import_module("state_codec").__file__),
        native_adapter_sha256=_sha256(importlib.import_module("native_adapter").__file__),
        recorder_capsule_sha256=_sha256(importlib.import_module("recorder_capsule").__file__),
        puct_sha256=_sha256(args.puct_root / "event_policy_rollout.py"),
        fair_native_sha256=_sha256(Path(__file__).with_name("native.py")),
        fair_belief_sha256=_sha256(Path(__file__).with_name("belief.py")),
        fair_draw_sha256=_sha256(Path(__file__).with_name("draw.py")),
        runtime_contract_sha256=_sha256(args.puct_root / "runtime_contract.py"),
        fair_transport_sha256=_sha256(Path(__file__).with_name("transport.py")),
        native_env_sha256=_sha256(Path(__file__).parents[2] / "cr_native_env.py"),
        batch_channel_sha256=_sha256(Path(__file__).parents[2] / "resident_batch_channel.py"),
        capture_decoder_sha256=_sha256(Path(__file__).parents[2] / "training_capture.py"),
        transport=args.transport,
        belief_mode=args.belief_mode,
        belief_reserve_worlds=args.belief_reserve_worlds,
        belief_attempts=args.belief_attempts,
        conditional_draws=args.conditional_draws,
        search_sha256=_sha256(Path(__file__).with_name("search.py")),
        fair_model_sha256=_sha256(Path(__file__).with_name("model.py")),
        fair_inference_sha256=_sha256(Path(__file__).with_name("inference.py")),
        collector_sha256=_sha256(Path(__file__).with_name("collector.py")),
        independent_visit_sampling=args.independent_visit_sampling,
        execution_only=not args.record_policy_steps,
        runtime_device=runtime_device,
        value_head=model.value_head_architecture,
    ))
    policies = {0: model, 1: model}
    identities = {0: checkpoint_sha256, 1: checkpoint_sha256}
    inference_options = dict(cache_entries=args.inference_cache_entries, score_batch=args.inference_score_batch,
                             fast_sampling=not args.reference_sampling)

    def make_inference():
        if args.inference_socket is not None:
            return SocketInferenceClient(args.inference_socket, checkpoint_sha256)
        return LocalInference(model, checkpoint_sha256, config=config, **inference_options)
    if args.replay_root is not None:
        saved = torch.load(args.replay_root, map_location="cpu", weights_only=True)
        information = InformationState.from_payload(saved["information"])
        factory = NativeReplayFactory(args.belief_port, policies, identities, rollout_type,
                                      diagnostic_directory=args.diagnostic_directory,
                                      search_only=args.search_only, transport=args.transport,
                                      conditional_draws=args.conditional_draws)
        started = time.perf_counter()
        if args.search_root:
            belief = ReplayBeliefProvider(factory, attempts=args.belief_attempts)
            inference = make_inference()
            try:
                result = InformationSetSearch(belief, inference, config=config).run(
                    information, seed=args.seed, training=False)
            finally:
                if args.inference_socket is not None:
                    inference.close()
            print(json.dumps(dict(root_sha256=information.sha256, visits=result.visits,
                                  selected=result.selected, particles=result.particles,
                                  seconds=time.perf_counter() - started,
                                  checkpoint_sha256=checkpoint_sha256,
                                  config_sha256=config.sha256, runtime_sha256=runtime_sha256,
                                  device=str(next(model.parameters()).device),
                                  search_only=args.search_only,
                                  timings=result.timings, inference_profile=getattr(inference, "profile", {}),
                                  inference_options=inference_options,
                                  trace_sha256=factory.completed_traces)), flush=True)
            return 0
        world = factory(information, seed=saved["seed"], ordinal=saved["ordinal"])
        try:
            world.restore_root()
            print(json.dumps(dict(root_sha256=world.root_information().sha256,
                                  seconds=time.perf_counter() - started)), flush=True)
        finally:
            world.close()
        return 0
    runtime = NativeMatchRuntime(args.main_port, policies, identities, rollout_type=rollout_type,
                                 seed=args.seed, match_id=args.episode_id or f"fair-smoke-{args.seed}",
                                 transport=args.transport, execution_only=not args.record_policy_steps)
    beliefs = {}
    inference = {}
    for owner in (0, 1):
        ports = args.opponent_belief_port if owner == 1 and args.belief_mode == "incremental" else args.belief_port
        factory = NativeReplayFactory(ports, policies, identities, rollout_type,
                                      diagnostic_directory=args.diagnostic_directory,
                                      search_only=args.search_only, transport=args.transport,
                                      conditional_draws=args.conditional_draws)
        provider_type = IncrementalBeliefProvider if args.belief_mode == "incremental" else ReplayBeliefProvider
        beliefs[owner] = (provider_type(factory, attempts=args.belief_attempts,
                                        reserve=args.belief_reserve_worlds)
                          if provider_type is IncrementalBeliefProvider
                          else provider_type(factory, attempts=args.belief_attempts))
        inference[owner] = make_inference()
    started = time.perf_counter()
    try:
        result = collect_episode(runtime, beliefs, inference,
                                 episode_id=args.episode_id or f"fair-smoke-{args.seed}",
                                 generation=args.generation, current_sha256=checkpoint_sha256,
                                 owner_sha256=identities, config=config, runtime_sha256=runtime_sha256,
                                 destination=args.output, attempt=args.attempt, max_seconds=args.max_seconds,
                                 independent_visit_sampling=args.independent_visit_sampling,
                                 progress=(lambda event: print(json.dumps(event, sort_keys=True), flush=True))
                                 if args.progress else None)
    finally:
        if args.inference_socket is not None:
            for client in inference.values():
                client.close()
    result.update(dict(checkpoint_sha256=checkpoint_sha256, config_sha256=config.sha256,
                       runtime_sha256=runtime_sha256, seed=args.seed, particles=args.particles,
                       device=str(next(model.parameters()).device),
                       wall_seconds=time.perf_counter() - started))
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
