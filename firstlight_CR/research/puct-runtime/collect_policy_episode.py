"""Collect a complete on-policy Hog episode, without search-selected PPO actions."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
if ROOT.name == "candidate":
    ROOT = ROOT.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime"), str(ROOT.parents[1])]

import torch
from runtime_contract import install
from server_engine import runtime, owned_alive
from event_policy_rollout import EventPolicyRollout
from teacher_targets import finish_episode
from native_runner.battle_env import BattleEnvV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.event_cache import default_event_contract


def export_completed_episode(recording, destination):
    """Export a saved completed recorder without replaying its native game."""
    destination = Path(destination)
    recorder, settled = recording["recorder"], recording["episode"]
    if recorder.open_rows or settled.get("ended") is not True or settled.get("truncated") is not False:
        raise ValueError("export requires a completed, untruncated recorder")
    summary = recorder.to_shard(destination / "part-00000.crev.zst", shard_id="part-00000",
                                contract=recording["contract"], owner=0)
    result = {key: value for key, value in recording.items() if key not in {"recorder", "contract"}}
    result.update(passed=True, shard=summary)
    (destination / "episode.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def opponent_identity(config, kind, learner_sha256):
    if kind not in config["round_games"]:
        raise ValueError("opponent kind is outside the formal allocation")
    if kind == "self_play":
        return learner_sha256, None
    frozen = config["opponent_checkpoints"][kind]
    return frozen["sha256"], frozen["iteration"]


def run(*, port, destination, seed, device="cuda:1", checkpoint=None,
        opponent_kind="self_play", max_seconds=3600, config_path=None, plan_path=None,
        expected_sha256=None, policy_pool=None):
    install()
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    config = json.loads(Path(config_path or ROOT / "config.json").read_text())
    plan = json.loads(Path(plan_path or ROOT / "match-plan.json").read_text())
    checkpoint = (ROOT / config["start_checkpoint"]).resolve() if checkpoint is None else Path(checkpoint)
    service = runtime()
    engine = service.load(ROOT / "engines" / str(port) / "process.json")
    if not owned_alive(service, engine, port) or not engine.get("ready"):
        raise RuntimeError("policy collection needs a ready task-owned engine")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    def report(value):
        value = dict(value, time=time.time())
        temporary = destination / "progress.tmp"
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(destination / "progress.json")
        print(json.dumps(value, allow_nan=False), flush=True)
    with (ROOT / "engines" / str(port) / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if policy_pool is None:
            policy = load_event_checkpoint(checkpoint, device=device)
        else:
            if expected_sha256 is None:
                raise ValueError("resident collection requires a pinned parent checkpoint")
            policy = policy_pool.load(checkpoint, device=device, expected_sha256=expected_sha256)
        if expected_sha256 is not None and policy.checkpoint_sha256 != expected_sha256:
            raise ValueError("PPO collection parent differs from the immutable job")
        opponent_sha256, opponent_iteration = opponent_identity(
            config, opponent_kind, policy.checkpoint_sha256)
        opponent = policy
        if opponent_kind != "self_play":
            opponent_path = ROOT.parent / "render-web" / "weights" / f"{opponent_sha256}.pt"
            if policy_pool is None:
                opponent = load_event_checkpoint(opponent_path, device=device)
            else:
                opponent = policy_pool.load(opponent_path, device=device,
                                            expected_sha256=opponent_sha256,
                                            expected_contract_sha256=policy.contract_sha256)
            if opponent.checkpoint_sha256 != opponent_sha256 or opponent.contract_sha256 != policy.contract_sha256:
                raise ValueError("frozen opponent identity or model contract differs from the allocation")
        report(dict(phase="collecting", mode="on-policy", checkpoint_sha256=policy.checkpoint_sha256,
                    opponent_kind=opponent_kind, opponent_sha256=opponent_sha256))
        native = NativeClashEnv(port=port, timeout=60)
        native._persistent_transport_requested = True
        try:
            rollout = EventPolicyRollout(
                BattleEnvV1(native=native), {0: policy, 1: opponent},
                reward=plan["source_training_state"]["reward_config"],
                match_id=f"hog-policy-{seed}",
            )
            rollout.reset(decks={owner: tuple(plan["learner_deck"]) for owner in (0, 1)}, seed=seed)
            settled = finish_episode(rollout, max_seconds=max_seconds, progress=report)
            contract = default_event_contract(policy.model.event_config)
            contract.update(collection_mode="on-policy", learner_deck_id="hog", search_enabled=False,
                            opponent_kind=opponent_kind, opponent_sha256=opponent_sha256,
                            opponent_iteration=opponent_iteration)
            recording = dict(mode="on-policy", episode=settled, recorder=rollout.recorder, contract=contract,
                             checkpoint_sha256=policy.checkpoint_sha256, contract_sha256=policy.contract_sha256,
                             rng_contract=("legacy" if policy_pool is None else
                                           "actor-seed-independent-of-checkpoint-loading.v1"),
                             execution=rollout.ledger.counters(), seed=seed, port=port,
                             opponent_kind=opponent_kind, opponent_sha256=opponent_sha256)
            temporary = destination / "completed-recorder.tmp"
            torch.save(recording, temporary)
            temporary.replace(destination / "completed-recorder.pt")
            result = export_completed_episode(recording, destination)
            report(dict(phase="complete", checkpoint_sha256=policy.checkpoint_sha256,
                        rows=rollout.recorder.lane_count(0), terminal=settled))
            return result
        finally:
            native.close_transport()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--device", default="cuda:1")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--opponent-kind", choices=("self_play", "fixed_1250", "recent"),
                        default="self_play")
    parser.add_argument("--max-seconds", type=float, default=3600.)
    run(**vars(parser.parse_args()))
