"""Expert-rollout distillation collector for the widened V4 schema.

Rolls out the migrated legacy checkpoint (the expert) in resident self-play,
records every decision's tensor record plus the expert's sampled action, and
packs both owners into the standard V4 IL cache format. The output directory
feeds train_imitation_cache.py directly:

    .venv/bin/python -m native_runner.training.v4.train_imitation_cache \
        --index runs/distill/cache/index.json \
        --train-tags runs/distill/cache/train-tags.txt \
        --output-dir runs/distill/train \
        --resume checkpoints/General/checkpoint-step-00000460.pt \
        --epochs 1 --gamma 0.99

Usage (master):
    .venv/bin/python tools/distill_collect.py \
        --checkpoint checkpoints/General/checkpoint-step-00000460.pt \
        --matches-per-lane 1 --output runs/distill
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.selfplay_resident import (  # noqa: E402
    ENGINES_PER_INSTANCE,
    INSTANCES,
    RESET_OPTIONS,
    control,
    restart_instance_engines,
)

GAMMA = 0.99
CHECKPOINT_CONTRACT_VERSION = "v4-il-cache-contract.v2"


def build_contract(expert_path: Path) -> dict[str, object]:
    from native_runner.training.v4.config import ModelConfigV4

    config_payload = json.dumps(asdict(ModelConfigV4()), sort_keys=True, separators=(",", ":")).encode()
    import hashlib

    return {
        "version": CHECKPOINT_CONTRACT_VERSION,
        "datasets": [],
        "model_config": asdict(ModelConfigV4()),
        "model_config_sha256": hashlib.sha256(config_payload).hexdigest(),
        "decision_ticks": ModelConfigV4().decision_ticks,
        "storage_float_dtype": "float16",
        "gamma_per_decision": GAMMA,
        "max_frames": None,
        "capture_mode": "compact",
        "headless": True,
        "pipeline_phase": "distillation-collection",
        "expert_checkpoint": str(expert_path),
    }


@dataclass
class LaneLog:
    env_id: int
    observations_by_owner: list = None
    actions_by_owner: list = None
    rewards_by_owner: list = None
    terminated_rows: list = None
    truncated_rows: list = None
    ticks: list = None

    def __post_init__(self) -> None:
        self.observations_by_owner = [[], []]
        self.actions_by_owner = [[], []]
        self.rewards_by_owner = [[], []]
        self.terminated_rows = []
        self.truncated_rows = []
        self.ticks = []

    def finalize(self, tag: str) -> object | None:
        from native_runner.training.v4.imitation import ILSequenceV4
        from native_runner.training.v4.learning import discounted_returns
        from native_runner.training.v4.producer import ReplayProductionResultV4

        time_steps = len(self.ticks)
        if time_steps == 0:
            return None
        import torch

        valid_mask = torch.ones(time_steps, 1, dtype=torch.bool)
        terminal = torch.tensor(self.terminated_rows, dtype=torch.bool)[:, None]
        truncation = torch.tensor(self.truncated_rows, dtype=torch.bool)[:, None]
        completed = bool(self.terminated_rows[-1] and not self.truncated_rows[-1])
        value_mask = valid_mask.clone() if completed else torch.zeros_like(valid_mask)
        sequences = []
        for owner in (0, 1):
            rewards = torch.tensor(self.rewards_by_owner[owner], dtype=torch.float32)[:, None]
            returns = discounted_returns(
                rewards,
                terminal,
                truncation,
                gamma=GAMMA,
                truncation_bootstrap_value=torch.zeros_like(rewards),
            )
            sequences.append(
                ILSequenceV4(
                    observations=tuple(self.observations_by_owner[owner]),
                    actions=tuple(self.actions_by_owner[owner]),
                    episode_start=torch.tensor([[step == 0] for step in range(time_steps)], dtype=torch.bool),
                    valid_mask=valid_mask,
                    returns=returns,
                    gate_loss_mask=valid_mask.clone(),
                    value_loss_mask=value_mask,
                    sequence_id=f"{tag}:owner-{owner}",
                    natural_time=True,
                )
            )
        expert_actions = sum(
            int((action.gate != 0).sum().item())
            for owner in (0, 1)
            for action in self.actions_by_owner[owner]
        )
        return ReplayProductionResultV4(
            replay_tag=tag,
            sequences=(sequences[0], sequences[1]),
            completed=completed,
            failure_reason=None if completed else "truncated-or-aborted",
            first_untrusted_tick=None,
            simulated_end_tick=int(self.ticks[-1]),
            source_end_tick=int(self.ticks[-1]),
            expert_action_count=expert_actions,
            executed_expert_action_count=expert_actions,
            winner_matches=None,
            crowns_match=None,
            decision_frame_count=time_steps,
        )


def worker() -> int:
    engine_index = int(os.environ["ENGINE_INDEX"])
    port = int(os.environ["HOST_PORT"])
    lanes_requested = int(os.environ.get("LANES", "8"))
    cache_dir = Path(os.environ["CACHE_DIR"])
    checkpoint = os.environ["CHECKPOINT"]
    matches_per_lane = int(os.environ.get("MATCHES_PER_LANE", "1"))
    stop_file = Path(os.environ["STOP_FILE"])
    engine_id = int(os.environ["ENGINE_ID"])

    import torch

    torch.set_num_threads(1)
    from native_runner.battle_env import BattleEnvV1
    from native_runner.contracts import ActionV1
    from native_runner.cr_native_env import ResidentNativeClashEnv
    from native_runner.match_factory import MatchConfig
    from native_runner.resident_batch_vector import ResidentBatchCoordinatorV1, ResidentBatchNativeProxyV1
    from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
    from native_runner.training.v4.cache import write_il_cache_shard
    from native_runner.training.v4.decoding import decode_action_sequence_v4
    from native_runner.training.v4.expert import FIRST_POLICY_DECISION_TICK, POLICY_DECISION_TICKS
    from native_runner.training.v4.factory import (
        build_episode_tensorizer_v4,
        production_semantic_bundle,
    )
    from native_runner.training.v4.policy_session import load_policy_v4
    from native_runner.training.v4.tensors import RecurrentPolicyStateV4, concatenate_padded_tensor_records

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    policy = load_policy_v4(checkpoint, device=device)
    model = policy.model
    bundle = production_semantic_bundle()
    natives = tuple(
        ResidentNativeClashEnv(env_id, "127.0.0.1", port, timeout=90.0) for env_id in range(lanes_requested)
    )
    coordinator = ResidentBatchCoordinatorV1(
        natives, timeout=90.0, training_capture=True, compressed_json=True, validate_json_capture=False
    )
    proxies = tuple(ResidentBatchNativeProxyV1(native, coordinator) for native in natives)
    environments = tuple(
        BattleEnvV1(
            native=proxy,
            card_catalog=bundle.native_card_catalog,
            semantic_subset_contract=bundle.semantic_subset_contract,
            effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
        )
        for proxy in proxies
    )
    executor = ThreadPoolExecutor(max_workers=lanes_requested, thread_name_prefix=f"distill-e{engine_index}")
    contract = build_contract(Path(checkpoint))
    cache_dir.mkdir(parents=True, exist_ok=True)
    tags_path = cache_dir / f"tags-e{engine_index:02d}.txt"
    tags_handle = tags_path.open("a", buffering=1)
    progress_path = cache_dir / f"progress-e{engine_index:02d}.json"

    results: list[object] = []
    summaries: list[dict[str, object]] = []
    shard_index = 0
    episode = 0
    completed_lanes = 0

    def progress(**data):
        data.update({"engine": engine_index, "wall": time.time()})
        progress_path.write_text(json.dumps(data))

    def flush_shards() -> None:
        nonlocal shard_index, results
        if not results:
            return
        summary = write_il_cache_shard(
            cache_dir / f"shard-e{engine_index:02d}-{shard_index:04d}.cril.zst",
            results,
            shard_id=f"distill-e{engine_index:02d}-{shard_index:04d}",
            contract=contract,
            compression_level=1,
            storage_float_dtype=torch.float16,
        )
        summaries.append(dict(summary))
        results = []
        shard_index += 1

    def decide_and_command(lanes, logs) -> tuple[dict[int, dict[int, tuple]], set[int]]:
        """Batch expert decisions; record frames; return commands + recorded lanes."""

        ready: list[tuple] = []
        for lane, log in zip(lanes, logs):
            for owner in (0, 1):
                actor = lane.actors[owner]
                observation = lane.observations[owner]
                if observation.tick >= FIRST_POLICY_DECISION_TICK:
                    ready.append((lane, log, owner, actor))
                else:
                    actor.tensorizer.tensorize(observation, validate=False)
        commands: dict[int, dict[int, tuple]] = {}
        recorded: set[int] = set()
        if ready:
            with torch.inference_mode():
                batches = tuple(
                    actor.tensorizer.tensorize(lane.observations[owner], validate=False)
                    for lane, _log, owner, actor in ready
                )
                combined = concatenate_padded_tensor_records(batches)
                stacked = RecurrentPolicyStateV4(
                    hidden=torch.cat(tuple(actor.state.hidden for _l, _g, _o, actor in ready)),
                    cell=torch.cat(tuple(actor.state.cell for _l, _g, _o, actor in ready)),
                )
                episode_start = torch.tensor(
                    [actor.first_decision for _l, _g, _o, actor in ready], dtype=torch.bool, device=device
                )
                output = model.sample_for_ppo_rollout(
                    combined.to_model_input(device), stacked, episode_start=episode_start, validate=False
                )
                for row, ((lane, log, owner, actor), host_batch) in enumerate(zip(ready, batches)):
                    action = output.actions.narrow_batch(row, 1)
                    actor.tensorizer.record_action(action, host_batch, row=0, validate=False)
                    decoded = decode_action_sequence_v4(
                        action,
                        host_batch.candidates,
                        row=0,
                        observation=lane.observations[owner],
                        catalog=actor.tensorizer.catalog,
                        deck=actor.tensorizer.deck,
                        card_costs=actor.tensorizer.card_costs,
                        ability_id_by_vocab_id=actor.tensorizer.ability_id_by_vocab_id,
                        horizontal_mirror=actor.tensorizer.perspective.horizontal_mirror,
                        hand_slot_permutation=actor.tensorizer.perspective.hand_slot_permutation,
                        config=actor.tensorizer.config,
                        base_latency_ticks=1,
                        base_latency_ms=0.0,
                        validate=False,
                    )
                    actor.state = output.next_state.narrow_batch(row, 1)
                    actor.first_decision = False
                    # Record the decision frame and the expert's sampled action.
                    log.observations_by_owner[owner].append(host_batch.to_storage("cpu", float_dtype=torch.float16))
                    log.actions_by_owner[owner].append(action.to_storage("cpu"))
                    recorded.add(lane.env_id)
                    commands.setdefault(lane.env_id, {})[owner] = tuple(decoded.actions)
        for lane in lanes:
            if lane.env_id in recorded:
                lane_log = next(log for log in logs if log.env_id == lane.env_id)
                lane_log.ticks.append(int(lane.observations[0].tick))
            lane_commands = commands.get(lane.env_id, {})
            for owner in (0, 1):
                lane_commands.setdefault(owner, (ActionV1.wait(owner, ticks=POLICY_DECISION_TICKS),))
            commands[lane.env_id] = lane_commands
        return commands, recorded

    @dataclass
    class ActorSlot:
        tensorizer: object
        state: object
        first_decision: bool = True

    @dataclass
    class MatchLane:
        env_id: int
        environment: object
        actors: list
        observations: dict

    try:
        while completed_lanes < lanes_requested * matches_per_lane and not stop_file.exists():
            coordinator.close_session()
            lanes = []
            logs = []
            episode_tags = [
                f"distill-e{engine_index:02d}-ep{episode:05d}-lane{lane_index}"
                for lane_index in range(lanes_requested)
            ]
            for env_id in range(lanes_requested):
                observations, _info = environments[env_id].reset(
                    match_config=MatchConfig(
                        deck0=[26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030],
                        deck1=[26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030],
                        seed=2_000_000 + engine_index * 1_000 + episode * 16 + env_id,
                    ),
                    options={**RESET_OPTIONS, "decision_ticks": POLICY_DECISION_TICKS},
                )
                initial_elixir = {
                    owner: next(p.elixir_exact for p in observations[owner].players if p.owner == owner)
                    for owner in (0, 1)
                }
                actors = []
                for owner in (0, 1):
                    tensorizer = build_episode_tensorizer_v4(
                        environments[env_id].episode_config, actor_owner=owner
                    )
                    tensorizer.start_episode(observations[owner], initial_elixir=initial_elixir)
                    actors.append(
                        ActorSlot(
                            tensorizer=tensorizer,
                            state=model.initial_state(1, device=device),
                        )
                    )
                lanes.append(
                    MatchLane(
                        env_id=env_id,
                        environment=environments[env_id],
                        actors=actors,
                        observations=observations,
                    )
                )
                logs.append(LaneLog(env_id=env_id))

            coordinator.start_session()
            active = list(range(lanes_requested))
            while active and not stop_file.exists():
                active_lanes = [lanes[index] for index in active]
                active_logs = [logs[index] for index in active]
                commands, recorded = decide_and_command(active_lanes, active_logs)
                coordinator.begin_round(tuple(lanes[index].env_id for index in active))
                futures = {
                    lanes[index].env_id: executor.submit(
                        lanes[index].environment.step,
                        commands[lanes[index].env_id],
                        record_trace=False,
                        policy_features_only=True,
                    )
                    for index in active
                }
                outcomes = {}
                for env_id, future in futures.items():
                    try:
                        outcomes[env_id] = future.result()
                    except BaseException as error:  # noqa: BLE001 - drop the lane below
                        outcomes[env_id] = error
                native_ok = coordinator.round_succeeded
                finish_error = None
                try:
                    coordinator.finish_round()
                except BaseException as error:  # noqa: BLE001
                    finish_error = error
                if not native_ok or finish_error is not None:
                    raise finish_error or RuntimeError("distill batch round failed")
                for index in active[:]:
                    log = logs[index]
                    lane = lanes[index]
                    outcome = outcomes[lane.env_id]
                    if isinstance(outcome, BaseException):
                        active.remove(index)
                        continue
                    observations, rewards, terms, truncs, _infos = outcome
                    lane.observations = observations
                    ended = bool(terms["__all__"]) or bool(truncs["__all__"])
                    if lane.env_id in recorded:
                        for owner in (0, 1):
                            log.rewards_by_owner[owner].append(float(rewards[owner]))
                        log.terminated_rows.append(bool(terms["__all__"]))
                        log.truncated_rows.append(bool(truncs["__all__"]))
                    if ended:
                        active.remove(index)
                        result = log.finalize(episode_tags[index])
                        if result is not None:
                            results.append(result)
                            tags_handle.write(episode_tags[index] + "\n")
                            completed_lanes += 1
            coordinator.close_session()
            episode += 1
            if len(results) >= 2 * lanes_requested:
                flush_shards()
            progress(episode=episode, lanes=completed_lanes, shards=shard_index)
    finally:
        tags_handle.close()
        try:
            flush_shards()
        except BaseException:  # noqa: BLE001
            pass
        try:
            coordinator.close_session()
        except BaseException:  # noqa: BLE001
            pass
        executor.shutdown(wait=True)
        progress(episode=episode, lanes=completed_lanes, shards=shard_index, done=True)
    return 0


def master(args) -> int:
    output = Path(args.output)
    cache_dir = output / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stop_file = output / "STOP"
    stop_file.unlink(missing_ok=True)
    engines = []
    for serial, base in INSTANCES:
        for engine_id in range(ENGINES_PER_INSTANCE):
            engines.append((len(engines), serial, base + engine_id, engine_id))
    if args.engines:
        selected = {int(value) for value in args.engines.split(",")}
        engines = [entry for entry in engines if entry[0] in selected]
    env_base = os.environ.copy()
    env_base.update(
        {
            "CHECKPOINT": args.checkpoint,
            "CACHE_DIR": str(cache_dir),
            "STOP_FILE": str(stop_file),
            "LANES": str(args.lanes),
            "MATCHES_PER_LANE": str(args.matches_per_lane),
            "CR_RELAX_RING_CURSORS": "1",
        }
    )

    def spawn(entry) -> subprocess.Popen:
        engine_index, serial, port, engine_id = entry
        env = dict(env_base)
        env.update(
            {
                "ENGINE_INDEX": str(engine_index),
                "ADB_SERIAL": serial,
                "HOST_PORT": str(port),
                "ENGINE_ID": str(engine_id),
                "CUDA_VISIBLE_DEVICES": str(engine_index % args.gpus),
            }
        )
        return subprocess.Popen(
            [sys.executable, __file__, "--worker"],
            env=env,
            stdout=(output / f"collect-{engine_index:02d}.log").open("a", buffering=1),
            stderr=subprocess.STDOUT,
        )

    for serial in sorted({entry[1] for entry in engines}):
        host_base = next(base for candidate, base in INSTANCES if candidate == serial)
        ready = True
        for engine_id in range(ENGINES_PER_INSTANCE):
            try:
                attestation = control(host_base + engine_id, "attest", timeout=8.0).get("attestation", {})
                ready = ready and attestation.get("production_ready") is True
            except Exception:  # noqa: BLE001
                ready = False
        if not ready:
            print(f"[{time.strftime('%H:%M:%S')}] {serial} not ready -> full engine restart", flush=True)
            restart_instance_engines(serial, host_base, range(ENGINES_PER_INSTANCE))
    procs = {entry[0]: spawn(entry) for entry in engines}
    print(f"distillation collection started: {len(engines)} engines x {args.lanes} lanes, "
          f"{args.matches_per_lane} matches/lane", flush=True)
    for engine_index, proc in procs.items():
        proc.wait()
    if stop_file.exists():
        print("STOP requested; collection may be incomplete", flush=True)

    tag_paths = sorted(cache_dir.glob("tags-e*.txt"))
    tags = []
    for path in tag_paths:
        tags.extend(row.strip() for row in path.read_text(encoding="utf-8").splitlines() if row.strip())
    tags = sorted(set(tags))
    if not tags:
        print("no distilled sequences were collected", flush=True)
        return 1
    (cache_dir / "train-tags.txt").write_text("\n".join(tags) + "\n", encoding="utf-8")

    from native_runner.training.v4.cache import write_il_cache_index

    summaries = []
    for path in sorted(cache_dir.glob("shard-*.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary.setdefault("summary_file", path.name)
        summaries.append(summary)
    index = write_il_cache_index(
        cache_dir / "index.json", summaries, contract=build_contract(Path(args.checkpoint))
    )
    print(
        json.dumps(
            {
                "ok": True,
                "tags": len(tags),
                "shards": len(summaries),
                "owner_frames": index.get("owner_frames"),
                "match_decision_frames": index.get("match_decision_frames"),
                "compressed_bytes": index.get("compressed_bytes"),
            }
        ),
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--checkpoint", default="checkpoints/General/checkpoint-step-00000460.pt")
    parser.add_argument("--output", default="runs/distill")
    parser.add_argument("--lanes", type=int, default=8)
    parser.add_argument("--matches-per-lane", type=int, default=1)
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--engines", default="", help="comma-separated engine indices (default all)")
    args = parser.parse_args()
    if args.worker:
        return worker()
    return master(args)


if __name__ == "__main__":
    raise SystemExit(main())
