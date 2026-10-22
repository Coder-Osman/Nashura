"""Real server-engine restoration checks with both recurrent 1755 policies."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(ROOT))

import torch

from runtime_contract import install

install()

from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from policy_rollout import DualPolicyRollout
from policy_signature import tensor_record_sha256
from server_engine import owned_alive, runtime
from tree_search import SearchState, state_digest
from native_runner.battle_env import BattleEnvV1
from native_runner.contracts import ActionV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import MatchConfig, tournament_level_kwargs
from native_runner.training.v4.expert import FIRST_POLICY_DECISION_TICK, POLICY_DECISION_TICKS
from native_runner.training.v4.policy_session import build_policy_session_v4, load_policy_v4


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"snapshotHandles", "stateEpoch"}}
    if isinstance(value, (tuple, list)):
        return [stable(item) for item in value]
    return value


def run(*, port: int, device: str, seed: int) -> dict:
    started = time.monotonic()
    service = runtime()
    owner = service.load(ROOT / "engines" / str(port) / "process.json")
    if not owned_alive(service, owner, port) or not owner.get("ready"):
        raise RuntimeError("task-owned server engine is not ready")
    config = json.loads((ROOT / "config.json").read_text())
    plan = json.loads((ROOT / "match-plan.json").read_text())
    checkpoint = (ROOT / config["start_checkpoint"]).resolve()
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    policy = load_policy_v4(checkpoint, device=device)
    if policy.checkpoint_sha256 != config["start_checkpoint_sha256"]:
        raise ValueError("acceptance checkpoint identity differs from 1755")
    native = NativeClashEnv(port=port, timeout=60)
    native._persistent_transport_requested = True
    saved = []
    rollout = None
    try:
        env = BattleEnvV1(native=native)
        match = MatchConfig(deck0=tuple(plan["learner_deck"]), deck1=tuple(plan["learner_deck"]),
                            seed=seed, **tournament_level_kwargs())
        observations, _ = env.reset(match_config=match, options={
            "render_mode": "headless", "decision_ticks": POLICY_DECISION_TICKS,
            "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
        initial = {o: next(p.elixir_exact for p in obs.players if p.owner == o)
                   for o, obs in observations.items()}
        sessions = {o: build_policy_session_v4(env, policy.model, actor_owner=o,
                    device=device, sample=False) for o in (0, 1)}
        rollout = DualPolicyRollout(env, sessions)
        rollout.start_episode(observations, initial_elixir=initial)
        while observations[0].tick < FIRST_POLICY_DECISION_TICK:
            for o in (0, 1):
                sessions[o].tensorizer.tensorize(observations[o], validate=False)
            observations, _, _, _, _ = env.step(
                {o: ActionV1.wait(o, ticks=POLICY_DECISION_TICKS) for o in (0, 1)},
                record_trace=False, policy_features_only=True)

        branch = {"mode": None}
        last = {"seconds": 0., "steps": []}

        def update_bookkeeping(rewards):
            rollout.event_cursor = {"combat": env._combat_next_sequence,
                "public_card_play": env._public_card_play_next_sequence, "events": len(env._events)}
            totals = rollout.reward_ledger.get("totals", {"0": 0., "1": 0.})
            rollout.reward_ledger = {"totals": {str(o): totals[str(o)] + float(rewards[o])
                                                 for o in (0, 1)}}

        update_bookkeeping({0: 0., 1: 0.})

        def capture():
            fair = {str(o): env.observe(owner=o, policy_features_only=True).to_dict() for o in (0, 1)}
            raw = stable(deepcopy(dict(env.raw_observation)))
            if raw.get("truncated") or any(obs["truncated"] for obs in fair.values()):
                raise ValueError("acceptance observation is truncated")
            return SearchState(native_snapshot={},
                observation={"tick": raw["tick"], "truncated": False, "native": raw, "fair": fair},
                actor=0, legal_actions=("policy-16", "wait-16"), external_queue=(),
                reward_ledger=deepcopy(rollout.reward_ledger), event_cursor=deepcopy(rollout.event_cursor),
                policy_state={"rollout": rollout.capture_search_policy_state(), "branch": branch["mode"]})

        def restore(state):
            rollout.restore_search_policy(state.policy_state["rollout"])
            branch["mode"] = state.policy_state["branch"]

        def apply(action):
            if action not in ("policy-16", "wait-16"):
                raise ValueError("unsupported acceptance branch")
            branch["mode"] = action

        def advance(max_points, max_seconds):
            if max_points < 16 or max_seconds < 16 * POLICY_DECISION_TICKS / 20:
                raise ValueError("acceptance branch exceeds requested horizon")
            start_tick = int(env.raw_observation["tick"])
            total = 0.
            steps = []
            for _ in range(16):
                step = {"tick": env.current_tick, "owners": {}}
                for o in (0, 1):
                    obs = env.observe(owner=o, policy_features_only=True)
                    host_batch = sessions[o].tensorizer.tensorize(obs, validate=False)
                    step["owners"][str(o)] = {"input": tensor_record_sha256(host_batch),
                        "mask": digest(obs.action_mask.to_dict())}
                    decision = rollout.decide(o, obs)
                    if branch["mode"] == "wait-16":
                        rollout.set_actions(o, (ActionV1.wait(o, ticks=POLICY_DECISION_TICKS),))
                    step["owners"][str(o)]["actions"] = [a.to_dict() for a in rollout.actions()[o]]
                _, rewards, terms, truncs, _ = env.step(rollout.actions(), record_trace=False,
                                                       policy_features_only=True)
                if terms["__all__"] or truncs["__all__"]:
                    raise ValueError("acceptance short branch unexpectedly ended")
                total += float(rewards[0])
                update_bookkeeping(rewards)
                step["policy"] = digest(rollout.capture_search_policy_state())
                step["event_cursor"] = deepcopy(rollout.event_cursor)
                step["native"] = digest(stable(dict(env.raw_observation)))
                steps.append(step)
            last.update(seconds=(int(env.raw_observation["tick"]) - start_tick) / 20, steps=steps)
            branch["mode"] = None
            return total, 16, last["seconds"]

        adapter = NativeSnapshotSearchAdapter(native, capture=capture, restore_sidecar=restore,
            apply=apply, advance=advance, battle_sidecar=BattleEnvSidecar(env))
        root = adapter.save_search_state()
        saved.append(root)
        root = SearchState.from_dict(json.loads(json.dumps(root.to_dict())))
        adapter.restore_search_state(root)
        fingerprint = adapter.fingerprint()
        branches = []
        detailed = []
        for action in ("policy-16", "wait-16", "policy-16"):
            adapter.restore_search_state(root)
            adapter.apply_action(action)
            result = adapter.advance_until_decision(16, 10.)
            saved.append(result.state)
            steps = deepcopy(last["steps"])
            receipts = {event.data.get("action_id") for event in env._events
                        if event.event_type == "action_executed"}
            rejections = {event.data.get("action_id") for event in env._events
                          if event.event_type == "action_rejected"}
            branches.append({"action": action, "tick": env.current_tick, "state": state_digest(result.state),
                "steps_sha256": digest(steps), "reward": result.reward,
                "confirmed_actions": len(receipts), "rejected_actions": len(rejections)})
            detailed.append(steps)
        if branches[0] != branches[2] or detailed[0] != detailed[2]:
            raise ValueError("dual-policy A/B/A differs: " + json.dumps(branches))
        if branches[0]["state"] == branches[1]["state"] or not branches[0]["confirmed_actions"]:
            raise ValueError("acceptance needs diverging branches and real native card receipts")
        if any(row["rejected_actions"] for row in branches):
            raise ValueError("native action rejection during policy restore acceptance")
        adapter.restore_search_state(saved[1])
        nested = adapter.save_search_state()
        saved.append(nested)
        adapter.restore_search_state(root)
        adapter.restore_search_state(nested)
        restored = adapter.save_search_state()
        saved.append(restored)
        if state_digest(restored) != state_digest(nested):
            raise ValueError("nested dual-policy restore differs")
        adapter.restore_search_state(root)
        if adapter.fingerprint() != fingerprint:
            raise ValueError("dual-policy root fingerprint differs after restore")
        for state in saved:
            adapter.release_search_state(state)
        if adapter.owned_snapshot_count:
            raise ValueError("dual-policy acceptance leaked native snapshot handles")
        return {"schema": "hog-dual-policy-acceptance.v1", "passed": True,
            "scope": "short native branches with both recurrent policies; not full-game PUCT acceptance",
            "seed": seed, "checkpoint_sha256": policy.checkpoint_sha256,
            "device": device, "port": port, "owner_pid": owner["pid"],
            "owner_start_ticks": owner["start_ticks"], "branches": branches,
            "json_restore": True, "nested_restore": True, "snapshot_handles_after": 0,
            "seconds": time.monotonic() - started}
    finally:
        if rollout is not None:
            rollout.end_episode()
        native.close_transport()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=29920)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--seed", type=int, default=1755)
    args = parser.parse_args()
    result = run(**vars(args))
    target = ROOT / "rollout-acceptance.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(target)
    print(json.dumps(result, indent=2), flush=True)
