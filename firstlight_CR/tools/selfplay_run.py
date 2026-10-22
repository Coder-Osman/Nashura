"""Unattended multi-slot self-play run: General vs General on the real engine.

Master: spawns one worker per engine slot, watches progress, respawns dead
workers, restarts wedged engines via adb, enforces the wall-clock deadline.
Worker: endless matches (both owners = the same checkpoint), streams slim
per-step trajectories and per-match summaries to disk.

Usage (master):
    python tools/selfplay_run.py --checkpoint checkpoints/General/checkpoint-step-00000460.pt \
        --hours 5 --output runs/selfplay
Worker mode is triggered by the SLOT/ADB_SERIAL/HOST_PORT environment the master sets.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DECK = [26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030]
INSTANCES = [("emulator-5554", 26789), ("emulator-5556", 26889), ("emulator-5558", 26989), ("emulator-5560", 27089)]
SLOTS_PER_INSTANCE = 6
ADB = os.environ.get("CR_TRAINING_ADB", "adb")


def worker() -> int:
    slot = int(os.environ["SLOT"])
    serial = os.environ["ADB_SERIAL"]
    port = int(os.environ["HOST_PORT"])
    checkpoint = os.environ["CHECKPOINT"]
    output = Path(os.environ["OUTPUT"])
    stop_file = Path(os.environ["STOP_FILE"])

    import torch

    torch.set_num_threads(1)
    from native_runner.battle_env import BattleEnvV1
    from native_runner.contracts import ActionKind, ActionV1
    from native_runner.cr_native_env import NativeClashEnv
    from native_runner.match_factory import MatchConfig
    from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
    from native_runner.training.v4.expert import FIRST_POLICY_DECISION_TICK, POLICY_DECISION_TICKS
    from native_runner.training.v4.factory import production_semantic_bundle
    from native_runner.training.v4.policy_session import build_policy_session_v4, load_policy_v4

    slot_dir = output / f"slot{slot:02d}"
    slot_dir.mkdir(parents=True, exist_ok=True)
    progress_path = slot_dir / "progress.json"
    traj_path = slot_dir / "trajectory.jsonl"
    summary_path = slot_dir / "matches.jsonl"
    fault_path = slot_dir / "FAULT"

    def progress(**data):
        data.update({"slot": slot, "wall": time.time()})
        progress_path.write_text(json.dumps(data))

    policy = load_policy_v4(checkpoint, device="cuda")
    bundle = production_semantic_bundle()
    native = NativeClashEnv("127.0.0.1", port, timeout=30.0)
    environment = BattleEnvV1(
        native=native,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
    )
    traj = traj_path.open("a", buffering=1)
    summaries = summary_path.open("a", buffering=1)

    match_index = 0
    consecutive_faults = 0
    try:
        while not stop_file.exists():
            if fault_path.exists():
                break
            seed = 1_000_000 + slot * 10_000 + match_index
            try:
                observations, _ = environment.reset(
                    match_config=MatchConfig(deck0=DECK, deck1=DECK, seed=seed),
                    options={
                        "render_mode": "headless",
                        "decision_ticks": POLICY_DECISION_TICKS,
                        "event_driven_decisions": False,
                        "allow_initial_remaining_runtime_rejections": True,
                    },
                )
                initial_elixir = {
                    o: next(p.elixir_exact for p in obs.players if p.owner == o)
                    for o, obs in observations.items()
                }
                sessions = [
                    build_policy_session_v4(environment, policy.model, actor_owner=o, device="cuda")
                    for o in (0, 1)
                ]
                for o, s in enumerate(sessions):
                    s.start_episode(observations[o], initial_elixir=initial_elixir)
                actions_by_owner = {0: 0, 1: 0}
                terminated = truncated = False
                while not terminated and not truncated and not stop_file.exists():
                    commands = {}
                    for owner, session in enumerate(sessions):
                        observation = observations[owner]
                        chosen = ()
                        if observation.tick >= FIRST_POLICY_DECISION_TICK:
                            chosen = session.decide(observation).decoded.actions
                            actions_by_owner[owner] += sum(a.kind != ActionKind.WAIT for a in chosen)
                        else:
                            session.tensorizer.tensorize(observation, validate=False)
                        commands[owner] = tuple(chosen) or (ActionV1.wait(owner, ticks=POLICY_DECISION_TICKS),)
                    observations, _, terms, truncs, _ = environment.step(commands, record_trace=False)
                    terminated, truncated = bool(terms["__all__"]), bool(truncs["__all__"])
                    slim = {
                        "tick": observations[0].tick,
                        "elixir": {p.owner: p.elixir_exact for p in observations[0].players},
                        "crowns": {p.owner: p.crowns for p in observations[0].players},
                        "towers": [
                            {"owner": t.owner, "kind": t.tower_kind, "hp": t.hitpoints, "max": t.max_hitpoints}
                            for t in observations[0].towers
                        ],
                        "entities": [
                            {"owner": e.owner, "card": e.card_id, "x": e.position[0], "y": e.position[1],
                             "hp": e.hitpoints, "max": e.max_hitpoints}
                            for e in observations[0].entities
                        ],
                    }
                    traj.write(json.dumps(
                        {"match": match_index, "seed": seed, **slim,
                         "actions": {o: [a.to_dict() for a in commands[o]] for o in (0, 1)}}
                    ) + "\n")
                terminal = observations[0].terminal
                summaries.write(json.dumps({
                    "slot": slot, "match": match_index, "seed": seed,
                    "winner": terminal.winner, "reason": terminal.reason,
                    "tick": observations[0].tick, "actions": actions_by_owner,
                }) + "\n")
                match_index += 1
                consecutive_faults = 0
                progress(match=match_index, last_tick=observations[0].tick, status="ok")
            except Exception as error:  # noqa: BLE001 - hands-off recovery
                consecutive_faults += 1
                progress(match=match_index, status="error", error=str(error)[:200], faults=consecutive_faults)
                if consecutive_faults >= 3:
                    fault_path.write_text(str(error))
                    break
                time.sleep(2.0)
    finally:
        traj.close()
        summaries.close()
        try:
            native.close_transport()
        except Exception:
            pass
    return 0 if not fault_path.exists() else 1


def engine_restart(serial: str, engine_id: int) -> None:
    subprocess.run([ADB, "-s", serial, "shell", "am", "force-stop", "nullsroyale.rel.free"],
                   capture_output=True, timeout=120)
    time.sleep(1.0)
    component = f"nullsroyale.rel.free/com.supercell.clashroyale.Engine{engine_id}App"
    subprocess.run([ADB, "-s", serial, "shell", "am", "start", "--user", "0", "-f", "0x18000000", "-n", component],
                   capture_output=True, timeout=120)


def master(args) -> int:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    stop_file = output / "STOP"
    stop_file.unlink(missing_ok=True)
    slots = []
    for serial, base in INSTANCES:
        for engine_id in range(SLOTS_PER_INSTANCE):
            slots.append((len(slots), serial, base + engine_id, engine_id))
    procs: dict[int, subprocess.Popen] = {}
    env_base = os.environ.copy()
    env_base["CHECKPOINT"] = args.checkpoint
    env_base["OUTPUT"] = str(output)
    env_base["STOP_FILE"] = str(stop_file)
    env_base["CR_RELAX_RING_CURSORS"] = "1"
    for slot, serial, port, engine_id in slots:
        env = dict(env_base)
        env["SLOT"] = str(slot)
        env["ADB_SERIAL"] = serial
        env["HOST_PORT"] = str(port)
        env["CUDA_VISIBLE_DEVICES"] = str(slot % 4)
        procs[slot] = subprocess.Popen(
            [sys.executable, __file__, "--worker"], env=env,
            stdout=(output / f"worker-{slot:02d}.log").open("w", buffering=1),
            stderr=subprocess.STDOUT,
        )
    deadline = time.time() + args.hours * 3600.0
    (output / "run.json").write_text(json.dumps({
        "checkpoint": args.checkpoint, "hours": args.hours, "slots": len(slots),
        "started": time.time(), "deadline": deadline,
    }))
    print(f"self-play started: {len(slots)} slots, checkpoint {args.checkpoint}, "
          f"deadline in {args.hours}h", flush=True)
    last_report = 0.0
    try:
        while time.time() < deadline:
            time.sleep(15)
            # restart wedged engines (per-slot fault markers)
            for slot, serial, port, engine_id in slots:
                fault = output / f"slot{slot:02d}" / "FAULT"
                if fault.exists():
                    print(f"[{time.strftime('%H:%M:%S')}] slot {slot} fault -> restart engine {engine_id} on {serial}", flush=True)
                    engine_restart(serial, engine_id)
                    time.sleep(20)
                    fault.unlink(missing_ok=True)
                    if procs[slot].poll() is not None:
                        env = dict(env_base)
                        env["SLOT"] = str(slot)
                        env["ADB_SERIAL"] = serial
                        env["HOST_PORT"] = str(port)
                        env["CUDA_VISIBLE_DEVICES"] = str(slot % 4)
                        procs[slot] = subprocess.Popen(
                            [sys.executable, __file__, "--worker"], env=env,
                            stdout=(output / f"worker-{slot:02d}.log").open("a", buffering=1),
                            stderr=subprocess.STDOUT,
                        )
            # respawn dead workers without fault markers
            for slot, serial, port, engine_id in slots:
                fault = output / f"slot{slot:02d}" / "FAULT"
                if not fault.exists() and procs[slot].poll() is not None:
                    env = dict(env_base)
                    env["SLOT"] = str(slot)
                    env["ADB_SERIAL"] = serial
                    env["HOST_PORT"] = str(port)
                    env["CUDA_VISIBLE_DEVICES"] = str(slot % 4)
                    procs[slot] = subprocess.Popen(
                        [sys.executable, __file__, "--worker"], env=env,
                        stdout=(output / f"worker-{slot:02d}.log").open("a", buffering=1),
                        stderr=subprocess.STDOUT,
                    )
            if time.time() - last_report > 300:
                last_report = time.time()
                matches = sum(
                    len((output / f"slot{s:02d}" / "matches.jsonl").open().readlines())
                    if (output / f"slot{s:02d}" / "matches.jsonl").exists() else 0
                    for s in range(len(slots))
                )
                print(f"[{time.strftime('%H:%M:%S')}] matches so far: {matches}", flush=True)
    finally:
        stop_file.write_text("done")
        for slot, proc in procs.items():
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
        matches = 0
        for s in range(len(slots)):
            path = output / f"slot{s:02d}" / "matches.jsonl"
            if path.exists():
                matches += len(path.open().readlines())
        print(f"self-play finished: {matches} matches in {args.hours}h", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--checkpoint", default="checkpoints/General/checkpoint-step-00000460.pt")
    parser.add_argument("--hours", type=float, default=5.0)
    parser.add_argument("--output", default="runs/selfplay")
    args = parser.parse_args()
    if args.worker:
        return worker()
    return master(args)


if __name__ == "__main__":
    raise SystemExit(main())
