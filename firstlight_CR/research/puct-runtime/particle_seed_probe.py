"""Measure whether native match seeds can produce legal hidden-state particles.

This is an acceptance probe, not a teacher-data generator.  It starts each
candidate from the same public match configuration and accepts a pair only if
owner 0's policy observation is byte-identical while owner 1's native hand is
different.  No native memory is edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(ROOT))

from runtime_contract import install

install()

from lifecycle import PORT, owned_live, record_read
from policy_signature import tensor_record_sha256
from native_runner.battle_env import BattleEnvV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import MatchConfig, tournament_level_kwargs
from native_runner.training.mixed_decks import DECKS
from native_runner.training.v4.factory import build_episode_tensorizer_v4


def canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def private_hand(observation: Mapping[str, Any], owner: int) -> tuple[tuple[int, int], ...]:
    player = next(item for item in observation["players"] if int(item["owner"]) == owner)
    return tuple((int(card["handIndex"]), int(card["cardId"])) for card in player["hand"])


def policy_input_sha256(env: BattleEnvV1, fair_observation: Any, owner: int) -> str:
    """Hash only actual V4 policy inputs; no session or native audit fields."""

    if env.episode_config is None:
        raise RuntimeError("episode config is missing after reset")
    tensorizer = build_episode_tensorizer_v4(env.episode_config, actor_owner=owner)
    return tensor_record_sha256(tensorizer.tensorize(fair_observation, validate=True))


def scan(limit: int, start_seed: int, ticks: int) -> dict[str, Any]:
    if not owned_live(record_read()):
        raise RuntimeError("task-owned native engine is not live")
    native = NativeClashEnv(port=PORT, timeout=30)
    native._persistent_transport_requested = True
    env = BattleEnvV1(native=native)
    started = time.monotonic()
    groups: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    try:
        for offset in range(limit):
            seed = start_seed + offset
            try:
                match = MatchConfig(deck0=DECKS["hog"], deck1=DECKS["hog"], seed=seed,
                                    **tournament_level_kwargs())
                env.reset(match_config=match)
                if ticks:
                    native.step(ticks)
                    env._raw = native.observe()
                fair_observation = env.observe(owner=0, policy_features_only=True)
                fair = fair_observation.to_dict()
                if fair.get("truncated"):
                    raise ValueError("fair observation was truncated")
                raw = native.observe()
                row = {
                    "seed": seed,
                    "tick": int(raw["tick"]),
                    "fair_observation_sha256": canonical_digest(fair),
                    "policy_input_sha256": policy_input_sha256(env, fair_observation, 0),
                    "owner0_hand": private_hand(raw, 0),
                    "owner1_hand": private_hand(raw, 1),
                    "state_sha256": str(native._request("state-digest")["sha256"]),
                }
                groups.setdefault(row["policy_input_sha256"], []).append(row)
            except Exception as error:  # Record every rejected seed for audit.
                failures.append({"seed": seed, "error": f"{type(error).__name__}: {error}"})
        candidates = []
        for policy_sha, rows in groups.items():
            by_hidden: dict[tuple[tuple[int, int], ...], list[dict[str, Any]]] = {}
            for row in rows:
                by_hidden.setdefault(tuple(row["owner1_hand"]), []).append(row)
            if len(by_hidden) > 1:
                candidates.append({
                    "policy_input_sha256": policy_sha,
                    "count": len(rows),
                    "owner0_hands": sorted({tuple(row["owner0_hand"]) for row in rows}),
                    "distinct_owner1_hands": sorted(by_hidden),
                    "representatives": [values[0] for values in by_hidden.values()],
                })
        return {
            "schema": "hog-hidden-particle-seed-probe.v1",
            "start_seed": start_seed,
            "attempted": limit,
            "ticks": ticks,
            "elapsed_seconds": time.monotonic() - started,
            "policy_input_groups": len(groups),
            "valid_hidden_particle_groups": candidates,
            "failures": failures,
            "engine_owner": record_read(),
        }
    finally:
        native.close_transport()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=128)
    parser.add_argument("--start-seed", type=int, default=1755)
    parser.add_argument("--ticks", type=int, default=20)
    args = parser.parse_args()
    if args.limit < 1 or args.ticks < 0:
        raise ValueError("limit must be positive and ticks must be non-negative")
    result = scan(args.limit, args.start_seed, args.ticks)
    target = ROOT / "particle-seed-probe.json"
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in ("engine_owner", "failures")},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
