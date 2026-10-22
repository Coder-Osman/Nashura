"""One owned-engine A/B/A check for native and BattleEnv branch restoration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PROJECT))

from runtime_contract import install

install()

from lifecycle import PORT, owned_live, record_read
from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from tree_search import SearchState, state_digest
from native_runner.battle_env import BattleEnvV1
from native_runner.contracts import ActionV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import MatchConfig, tournament_level_kwargs
from native_runner.training.mixed_decks import DECKS


def digest(value: object) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items()
                 if key not in ("snapshotHandles", "stateEpoch")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def run() -> dict:
    if not owned_live(record_read()):
        raise RuntimeError("task-owned native engine is not live")
    native = NativeClashEnv(port=PORT, timeout=30)
    native._persistent_transport_requested = True
    states = []
    try:
        env = BattleEnvV1(native=native)
        match = MatchConfig(deck0=DECKS["hog"], deck1=DECKS["hog"], seed=1755,
                            **tournament_level_kwargs())
        env.reset(match_config=match)
        sidecar = BattleEnvSidecar(env)
        pending = {"action": None}
        last = {"reward": 0.0, "seconds": 0.0}

        def capture() -> SearchState:
            observation = env.observe(owner=0, policy_features_only=True).to_dict()
            return SearchState(
                native_snapshot={}, observation=observation, actor=0,
                legal_actions=("wait-20", "wait-40"), external_queue=(),
                reward_ledger={"last_reward": last["reward"]},
                event_cursor={"combat": env._combat_next_sequence,
                              "public_card_play": env._public_card_play_next_sequence,
                              "events": len(env._events)},
                policy_state={"pending": pending["action"]})

        def restore(state):
            pending["action"] = state.policy_state["pending"]
            last["reward"] = state.reward_ledger["last_reward"]

        def apply(action: str):
            if action not in ("wait-20", "wait-40"):
                raise ValueError("illegal acceptance action")
            pending["action"] = action

        def advance(max_points: int, max_seconds: float):
            ticks = int(pending["action"].removeprefix("wait-"))
            if max_points < 1 or ticks / 20 > max_seconds:
                raise ValueError("acceptance step exceeds horizon")
            before = int(env.raw_observation["tick"])
            _, rewards, _, _, _ = env.step(
                {0: ActionV1.wait(0, ticks=ticks), 1: ActionV1.wait(1, ticks=ticks)},
                record_trace=False, policy_features_only=True)
            after = int(env.raw_observation["tick"])
            last["reward"] = float(rewards[0])
            last["seconds"] = (after - before) / 20
            pending["action"] = None
            return last["reward"], 1, last["seconds"]

        adapter = NativeSnapshotSearchAdapter(
            native, capture=capture, restore_sidecar=restore,
            apply=apply, advance=advance, battle_sidecar=sidecar)
        root = adapter.save_search_state()
        states.append(root)
        baseline = adapter.fingerprint()
        branches = []
        for action in ("wait-20", "wait-40", "wait-20"):
            adapter.restore_search_state(root)
            adapter.apply_action(action)
            child = adapter.advance_until_decision(6, 10)
            states.append(child.state)
            branches.append({"state": state_digest(child.state),
                             "raw": digest(env.raw_observation),
                             "reward": child.reward,
                             "tick": env.raw_observation["tick"]})
        adapter.restore_search_state(root)
        restored_fingerprint = adapter.fingerprint()
        if restored_fingerprint != baseline or branches[0] != branches[2]:
            raise ValueError("real BattleEnv A/B/A restore mismatch: " + json.dumps({
                "root_fingerprint_before": baseline,
                "root_fingerprint_after": restored_fingerprint,
                "branch_a_first": branches[0],
                "branch_a_second": branches[2],
            }, sort_keys=True))
        if branches[0]["state"] == branches[1]["state"]:
            raise ValueError("A and B branches did not diverge")
        adapter.restore_search_state(states[1])
        nested = adapter.save_search_state()
        states.append(nested)
        adapter.restore_search_state(root)
        adapter.restore_search_state(nested)
        if state_digest(adapter.save_search_state()) != state_digest(nested):
            raise ValueError("nested BattleEnv restore mismatch")
        states.append(adapter._active)
        adapter.restore_search_state(root)
        for state in states:
            adapter.release_search_state(state)
        if adapter.owned_snapshot_count:
            raise ValueError("native snapshot handle leak")
        return {"schema": "hog-tree-native-acceptance.v1", "passed": True,
                "scope": "native and BattleEnv A/B/A with deterministic complete state",
                "root_tick": root.observation["tick"], "branches": branches,
                "snapshot_handles_after": adapter.owned_snapshot_count,
                "engine_owner": record_read()}
    finally:
        native.close_transport()


if __name__ == "__main__":
    result = run()
    target = ROOT / "native-acceptance.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "engine_owner"}, indent=2))
