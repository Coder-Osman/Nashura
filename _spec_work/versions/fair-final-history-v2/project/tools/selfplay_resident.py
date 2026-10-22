"""Resident multi-match self-play: one worker per engine, N lanes per worker.

Phase 1 migration off the legacy one-match-per-port runner: each worker owns
one engine process and drives up to 16 resident matches over the binary batch
data plane (one round trip advances every lane five ticks), with one batched
GPU forward for all actors per decision round.

Master: spawns one worker per engine, watches progress, restarts wedged
engines, respawns workers, enforces the wall-clock deadline.
Worker: endless episodes (both owners = the same checkpoint), per-match
summaries to disk, optional slim trajectories.

Usage (master):
    .venv/bin/python tools/selfplay_resident.py \
        --checkpoint checkpoints/General/checkpoint-step-00000460.pt \
        --hours 5 --output runs/selfplay-resident --lanes 8
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DECK = [26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030]
INSTANCES = [
    ("emulator-5554", 26789),
    ("emulator-5556", 26889),
    ("emulator-5558", 26989),
    ("emulator-5560", 27089),
]
ENGINES_PER_INSTANCE = 6
PACKAGE = "nullsroyale.rel.free"
ADB = os.environ.get("CR_TRAINING_ADB", "adb")

RESET_OPTIONS = {
    "render_mode": "headless",
    "event_driven_decisions": False,
    "allow_initial_remaining_runtime_rejections": True,
}


@dataclass
class ActorLane:
    """Per-owner recurrent inference bookkeeping for one resident lane."""

    tensorizer: object
    state: object = None
    first_decision: bool = True
    last_batch: object = None
    nonwait_actions: int = 0


@dataclass
class MatchLane:
    env_id: int
    environment: object
    actors: list
    observations: dict = field(default_factory=dict)
    active: bool = True
    decision_steps: int = 0


def worker() -> int:
    engine_index = int(os.environ["ENGINE_INDEX"])
    serial = os.environ["ADB_SERIAL"]
    port = int(os.environ["HOST_PORT"])
    engine_id = int(os.environ["ENGINE_ID"])
    lanes_requested = int(os.environ.get("LANES", "8"))
    checkpoint = os.environ["CHECKPOINT"]
    output = Path(os.environ["OUTPUT"])
    stop_file = Path(os.environ["STOP_FILE"])
    keep_trajectory = os.environ.get("KEEP_TRAJECTORY", "") == "1"

    import torch

    torch.set_num_threads(1)
    from native_runner.battle_env import BattleEnvV1
    from native_runner.contracts import ActionKind, ActionV1
    from native_runner.cr_native_env import ResidentNativeClashEnv
    from native_runner.match_factory import MatchConfig, tournament_level_kwargs
    from native_runner.resident_batch_vector import ResidentBatchCoordinatorV1, ResidentBatchNativeProxyV1
    from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
    from native_runner.training.v4.decoding import decode_action_sequence_v4
    from native_runner.training.v4.expert import FIRST_POLICY_DECISION_TICK, POLICY_DECISION_TICKS
    from native_runner.training.v4.factory import (
        build_episode_tensorizer_v4,
        production_semantic_bundle,
    )
    from native_runner.training.v4.policy_session import load_policy_v4
    from native_runner.training.v4.tensors import RecurrentPolicyStateV4, concatenate_padded_tensor_records

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    engine_dir = output / f"engine{engine_index:02d}"
    engine_dir.mkdir(parents=True, exist_ok=True)
    progress_path = engine_dir / "progress.json"
    summaries_path = engine_dir / "matches.jsonl"
    trajectory_path = engine_dir / "trajectory.jsonl"
    fault_path = engine_dir / "FAULT"

    def progress(**data):
        data.update({"engine": engine_index, "wall": time.time()})
        progress_path.write_text(json.dumps(data))

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
    executor = ThreadPoolExecutor(max_workers=lanes_requested, thread_name_prefix=f"resident-e{engine_index}")
    summaries = summaries_path.open("a", buffering=1)
    trajectory = trajectory_path.open("a", buffering=1) if keep_trajectory else None

    env_timing: dict[str, float] = {}
    if os.environ.get("CR_ENV_TIMING", "") == "1":

        def _wrap_timing(label: str, target):
            def wrapper(self, *method_args, **method_kwargs):
                started = time.perf_counter()
                try:
                    return target(self, *method_args, **method_kwargs)
                finally:
                    key = f"{label}_s"
                    env_timing[key] = env_timing.get(key, 0.0) + time.perf_counter() - started
                    env_timing[f"{label}_calls"] = env_timing.get(f"{label}_calls", 0.0) + 1

            return wrapper

        for label, method in (
            ("observe", "observe"),
            ("observe_dual", "observe_dual"),
            ("entities", "_entities"),
            ("players", "_players"),
            ("mask", "action_mask"),
            ("events_for", "_events_for"),
        ):
            if hasattr(BattleEnvV1, method):
                setattr(BattleEnvV1, method, _wrap_timing(label, getattr(BattleEnvV1, method)))

    def decide_and_command(lanes: list[MatchLane]) -> dict[int, dict[int, tuple]]:
        """One batched forward for every ready actor; returns commands per lane."""

        ready: list[tuple[MatchLane, int, ActorLane]] = []
        warm: list[tuple[MatchLane, int, ActorLane]] = []
        for lane in lanes:
            for owner in (0, 1):
                actor = lane.actors[owner]
                observation = lane.observations[owner]
                if observation.tick >= FIRST_POLICY_DECISION_TICK:
                    ready.append((lane, owner, actor))
                else:
                    warm.append((lane, owner, actor))

        tensorize_started = time.perf_counter()
        for lane, owner, actor in warm:
            actor.tensorizer.tensorize(lane.observations[owner], validate=False)
        batches = tuple(
            actor.tensorizer.tensorize(lane.observations[owner], validate=False)
            for lane, owner, actor in ready
        )
        tensorize_s = time.perf_counter() - tensorize_started
        commands: dict[int, dict[int, tuple]] = {}
        if ready:
            with torch.inference_mode():
                forward_started = time.perf_counter()
                forward_started = time.perf_counter()
                combined = concatenate_padded_tensor_records(batches)
                stacked = RecurrentPolicyStateV4(
                    hidden=torch.cat(tuple(actor.state.hidden for _l, _o, actor in ready)),
                    cell=torch.cat(tuple(actor.state.cell for _l, _o, actor in ready)),
                )
                episode_start = torch.tensor(
                    [actor.first_decision for _l, _o, actor in ready], dtype=torch.bool, device=device
                )
                output = model.sample_for_ppo_rollout(
                    combined.to_model_input(device), stacked, episode_start=episode_start, validate=False
                )
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                forward_s = time.perf_counter() - forward_started
                decode_started = time.perf_counter()
                for row, ((lane, owner, actor), host_batch) in enumerate(zip(ready, batches)):
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
                    chosen = tuple(decoded.actions)
                    actor.nonwait_actions += sum(item.kind != ActionKind.WAIT for item in chosen)
                    commands.setdefault(lane.env_id, {})[owner] = chosen
        for lane in lanes:
            lane_commands = commands.get(lane.env_id, {})
            for owner in (0, 1):
                lane_commands.setdefault(
                    owner, (ActionV1.wait(owner, ticks=POLICY_DECISION_TICKS),)
                )
            commands[lane.env_id] = lane_commands
        if ready:
            decode_s = time.perf_counter() - decode_started
            timing = {
                "actors": len(ready),
                "tensorize_s": tensorize_s,
                "forward_s": forward_s,
                "decode_s": decode_s,
            }
        else:
            timing = {"actors": 0, "tensorize_s": tensorize_s, "forward_s": 0.0, "decode_s": 0.0}
        return commands, timing

    def run_episode(episode: int) -> None:
        coordinator.close_session()
        lanes: list[MatchLane] = []
        for env_id in range(lanes_requested):
            observations, _info = environments[env_id].reset(
                match_config=MatchConfig(
                    deck0=DECK,
                    deck1=DECK,
                    seed=1_000_000 + engine_index * 1_000 + episode * 16 + env_id,
                    **tournament_level_kwargs(),
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
                    ActorLane(tensorizer=tensorizer, state=model.initial_state(1, device=device))
                )
            lanes.append(
                MatchLane(env_id=env_id, environment=environments[env_id], actors=actors, observations=observations)
            )

        coordinator.start_session()
        active = list(lanes)
        rounds = 0
        timing_sums = {"tensorize_s": 0.0, "forward_s": 0.0, "decode_s": 0.0, "round_s": 0.0}
        while active and not stop_file.exists():
            round_started = time.perf_counter()
            commands, timing = decide_and_command(active)
            if trajectory is not None:
                for lane in active:
                    observation = lane.observations[0]
                    slim = {
                        "engine": engine_index,
                        "env": lane.env_id,
                        "episode": episode,
                        "tick": observation.tick,
                        "elixir": {p.owner: p.elixir_exact for p in observation.players},
                        "crowns": {p.owner: p.crowns for p in observation.players},
                        "towers": [
                            {"owner": t.owner, "kind": t.tower_kind, "hp": t.hitpoints}
                            for t in observation.towers
                        ],
                        "entities": [
                            {"owner": e.owner, "card": e.card_id, "x": e.position[0], "y": e.position[1],
                             "hp": e.hitpoints}
                            for e in observation.entities
                        ],
                        "actions": {
                            o: [a.to_dict() for a in commands[lane.env_id][o]] for o in (0, 1)
                        },
                    }
                    trajectory.write(json.dumps(slim) + "\n")
            coordinator.begin_round(tuple(lane.env_id for lane in active))
            futures = {
                lane.env_id: executor.submit(
                    lane.environment.step, commands[lane.env_id], record_trace=False, policy_features_only=True
                )
                for lane in active
            }
            results: dict[int, tuple] = {}
            errors: dict[int, BaseException] = {}
            for env_id, future in futures.items():
                try:
                    results[env_id] = future.result()
                except BaseException as error:  # noqa: BLE001 - drop or fail below
                    errors[env_id] = error
            native_ok = coordinator.round_succeeded
            finish_error: BaseException | None = None
            try:
                coordinator.finish_round()
            except BaseException as error:  # noqa: BLE001
                finish_error = error
            if not native_ok or finish_error is not None:
                raise (finish_error or RuntimeError("resident batch round failed"))
            for lane in active[:]:
                if lane.env_id in errors:
                    summaries.write(
                        json.dumps(
                            {
                                "engine": engine_index,
                                "env": lane.env_id,
                                "episode": episode,
                                "error": str(errors[lane.env_id])[:200],
                            }
                        )
                        + "\n"
                    )
                    lane.active = False
                    active.remove(lane)
                    continue
                observations, _rewards, terms, truncs, _infos = results[lane.env_id]
                lane.observations = observations
                lane.decision_steps += 1
                if bool(terms["__all__"]) or bool(truncs["__all__"]):
                    terminal = observations[0].terminal
                    summaries.write(
                        json.dumps(
                            {
                                "engine": engine_index,
                                "env": lane.env_id,
                                "episode": episode,
                                "winner": terminal.winner,
                                "reason": terminal.reason,
                                "truncated": bool(truncs["__all__"]),
                                "tick": observations[0].tick,
                                "rounds": lane.decision_steps,
                                "crowns": {
                                    p.owner: int(p.crowns) for p in observations[0].players
                                },
                                "actions": [lane.actors[0].nonwait_actions, lane.actors[1].nonwait_actions],
                            }
                        )
                        + "\n"
                    )
                    lane.active = False
                    active.remove(lane)
            rounds += 1
            timing_sums["round_s"] += time.perf_counter() - round_started
            for key in ("tensorize_s", "forward_s", "decode_s"):
                timing_sums[key] += timing[key]
            if rounds % 50 == 0:
                per_round = {key: round(value / rounds, 4) for key, value in timing_sums.items()}
                report = {
                    "status": "ok",
                    "episode": episode,
                    "round": rounds,
                    "active": len(active),
                    "per_round": per_round,
                }
                if env_timing:
                    per_call = {}
                    for key in list(env_timing):
                        if key.endswith("_calls"):
                            continue
                        calls = env_timing.get(f"{key[:-2]}_calls", 0)
                        if calls:
                            per_call[key] = round(env_timing[key] / calls * 1000.0, 2)
                    report["env_ms_per_call"] = per_call
                progress(**report)
        coordinator.close_session()

    episode = 0
    consecutive_faults = 0
    started = time.perf_counter()
    matches_written = 0
    try:
        while not stop_file.exists():
            if fault_path.exists():
                break
            try:
                run_episode(episode)
                episode += 1
                consecutive_faults = 0
                matches = sum(1 for _ in summaries_path.open())
                rate = (matches - matches_written) / max(time.perf_counter() - started, 1e-9)
                matches_written = matches
                started = time.perf_counter()
                progress(status="ok", episode=episode, matches=matches, match_rate_per_min=round(rate * 60, 2))
            except Exception as error:  # noqa: BLE001 - hands-off recovery
                consecutive_faults += 1
                progress(
                    status="error", episode=episode, error=str(error)[:200], faults=consecutive_faults
                )
                if consecutive_faults >= 3:
                    fault_path.write_text(str(error))
                    break
                time.sleep(2.0)
    finally:
        summaries.close()
        if trajectory is not None:
            trajectory.close()
        try:
            coordinator.close_session()
        except Exception:  # noqa: BLE001
            pass
        executor.shutdown(wait=True)
    return 0 if not fault_path.exists() else 1


def start_engine(serial: str, engine_id: int) -> None:
    subprocess.run(
        [ADB, "-s", serial, "shell", "am", "start", "--user", "0", "-f", "0x18000000", "-n",
         f"{PACKAGE}/com.supercell.clashroyale.Engine{engine_id}App"],
        capture_output=True, timeout=120,
    )


def control(port: int, command: str, timeout: float = 8.0) -> dict:
    import socket as socket_module

    with socket_module.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((command + "\n").encode("utf-8"))
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    import json as json_module

    raw = b"".join(chunks).decode("utf-8").strip()
    if not raw:
        raise RuntimeError(f"port {port} returned no data for {command}")
    return json_module.loads(raw)


def wait_engine_ready(port: int, engine_id: int, timeout: float = 240.0) -> None:
    """Cold libndk translation takes minutes; attest gates production use."""

    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            status = control(port, "engine-status", timeout=3.0)
            attestation = control(port, "attest", timeout=8.0).get("attestation", {})
            checks = {
                "ok": status.get("ok") is True,
                "engine_id": status.get("engineId") == engine_id,
                "production_ready": attestation.get("production_ready") is True,
            }
            failed = [name for name, passed in checks.items() if not passed]
            if failed:
                raise RuntimeError(f"failed checks {failed}")
            control(port, "render off", timeout=3.0)
            return
        except Exception as error:  # noqa: BLE001 - readiness polling keeps the last cause
            last_error = repr(error)
            time.sleep(2.0)
    raise RuntimeError(f"engine {engine_id} on port {port} not ready: {last_error}")


def restart_instance_engines(serial: str, host_base: int, engine_ids: range) -> None:
    """Package force-stop kills every engine on the serial; restart them all."""

    subprocess.run([ADB, "-s", serial, "shell", "settings", "put", "global",
                    "cached_apps_freezer", "disabled"], capture_output=True, timeout=60)
    subprocess.run([ADB, "-s", serial, "shell", "am", "force-stop", PACKAGE], capture_output=True, timeout=120)
    time.sleep(1.5)
    for engine_id in engine_ids:
        start_engine(serial, engine_id)
        try:
            wait_engine_ready(host_base + engine_id, engine_id)
        except RuntimeError as error:
            print(f"engine {engine_id} on {serial} restart problem: {error}", flush=True)
        time.sleep(1.0)


def master(args) -> int:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    stop_file = output / "STOP"
    stop_file.unlink(missing_ok=True)
    engines = []
    for serial, base in INSTANCES:
        for engine_id in range(ENGINES_PER_INSTANCE):
            engines.append((len(engines), serial, base + engine_id, engine_id))
    if args.engines:
        selected = {int(value) for value in args.engines.split(",")}
        engines = [item for item in engines if item[0] in selected]
    procs: dict[int, subprocess.Popen] = {}
    env_base = os.environ.copy()
    env_base.update(
        {
            "CHECKPOINT": args.checkpoint,
            "OUTPUT": str(output),
            "STOP_FILE": str(stop_file),
            "LANES": str(args.lanes),
            "KEEP_TRAJECTORY": "1" if args.keep_trajectory else "",
            "CR_RELAX_RING_CURSORS": "1",
        }
    )

    def spawn(engine_entry) -> subprocess.Popen:
        engine_index, serial, port, engine_id = engine_entry
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
            stdout=(output / f"worker-{engine_index:02d}.log").open("a", buffering=1),
            stderr=subprocess.STDOUT,
        )

    # Startup gate: workers attest on their first reset, so every engine must
    # already be production-ready (cold translation warmup takes minutes).
    for serial in sorted({entry[1] for entry in engines}):
        host_base = next(base for candidate, base in INSTANCES if candidate == serial)
        ready = True
        for engine_id in range(ENGINES_PER_INSTANCE):
            try:
                attestation = control(host_base + engine_id, "attest", timeout=8.0).get("attestation", {})
                ready = ready and attestation.get("production_ready") is True
            except Exception:  # noqa: BLE001 - fall through to the full restart
                ready = False
        if not ready:
            print(f"[{time.strftime('%H:%M:%S')}] {serial} not ready -> full engine restart", flush=True)
            restart_instance_engines(serial, host_base, range(ENGINES_PER_INSTANCE))
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {serial} engines production-ready", flush=True)
    deadline = time.time() + args.hours * 3600.0
    for entry in engines:
        procs[entry[0]] = spawn(entry)
    (output / "run.json").write_text(
        json.dumps(
            {
                "checkpoint": args.checkpoint,
                "hours": args.hours,
                "engines": [entry[0] for entry in engines],
                "lanes": args.lanes,
                "started": time.time(),
                "deadline": deadline,
            }
        )
    )
    print(
        f"resident self-play started: {len(engines)} engines x {args.lanes} lanes, "
        f"deadline in {args.hours}h",
        flush=True,
    )
    serials = {serial for _idx, serial, _port, _eid in engines}
    per_serial_engines = {
        serial: [entry for entry in engines if entry[1] == serial] for serial in serials
    }
    serial_restarting = {serial: False for serial in serials}
    restart_lock = threading.Lock()

    def handle_serial_faults(serial: str) -> None:
        with restart_lock:
            if serial_restarting[serial]:
                return
            serial_restarting[serial] = True
        try:
            host_base = next(base for candidate, base in INSTANCES if candidate == serial)
            restart_instance_engines(serial, host_base, range(ENGINES_PER_INSTANCE))
            for entry in per_serial_engines[serial]:
                fault = output / f"engine{entry[0]:02d}" / "FAULT"
                fault.unlink(missing_ok=True)
                if procs[entry[0]].poll() is not None:
                    procs[entry[0]] = spawn(entry)
                    print(f"[{time.strftime('%H:%M:%S')}] respawned worker engine{entry[0]:02d}", flush=True)
        finally:
            with restart_lock:
                serial_restarting[serial] = False

    last_report = 0.0
    try:
        while time.time() < deadline:
            time.sleep(15)
            now = time.time()
            # engine-level faults: one background restart per serial at a time
            for serial in serials:
                has_fault = any(
                    (output / f"engine{entry[0]:02d}" / "FAULT").exists()
                    for entry in per_serial_engines[serial]
                )
                if has_fault and not serial_restarting[serial]:
                    print(f"[{time.strftime('%H:%M:%S')}] faults on {serial} -> restarting its engines", flush=True)
                    threading.Thread(target=handle_serial_faults, args=(serial,), daemon=True).start()
            # respawn workers that died without a fault marker (transient crashes)
            for entry in engines:
                engine_index = entry[0]
                fault = output / f"engine{engine_index:02d}" / "FAULT"
                if procs[engine_index].poll() is not None and not fault.exists():
                    procs[engine_index] = spawn(entry)
                    print(f"[{time.strftime('%H:%M:%S')}] respawned worker engine{engine_index:02d}", flush=True)
            if now - last_report > 300:
                last_report = now
                matches = 0
                for entry in engines:
                    path = output / f"engine{entry[0]:02d}" / "matches.jsonl"
                    if path.exists():
                        matches += sum(1 for _ in path.open())
                print(f"[{time.strftime('%H:%M:%S')}] matches so far: {matches}", flush=True)
    finally:
        stop_file.write_text("done")
        for engine_index, proc in procs.items():
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                proc.kill()
        matches = 0
        for entry in engines:
            path = output / f"engine{entry[0]:02d}" / "matches.jsonl"
            if path.exists():
                matches += sum(1 for _ in path.open())
        print(f"resident self-play finished: {matches} matches in {args.hours}h", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--checkpoint", default="checkpoints/General/checkpoint-step-00000460.pt")
    parser.add_argument("--hours", type=float, default=5.0)
    parser.add_argument("--output", default="runs/selfplay-resident")
    parser.add_argument("--lanes", type=int, default=8)
    parser.add_argument("--gpus", type=int, default=4)
    parser.add_argument("--engines", default="", help="comma-separated engine indices to use (default all)")
    parser.add_argument("--keep-trajectory", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return worker()
    return master(args)


if __name__ == "__main__":
    raise SystemExit(main())
