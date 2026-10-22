"""Real event-policy A/B/A with delayed plans, receipts and open reward rows."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runtime"))

os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
import torch
from runtime_contract import install

install()

from event_policy_rollout import EventPolicyRollout, action_key
from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from policy_signature import tensor_record_sha256
from server_engine import owned_alive, runtime
from tree_search import SearchState, state_digest
from native_runner.battle_env import BattleEnvV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.training.event_checkpoint import load_event_checkpoint


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"snapshotHandles", "stateEpoch"}}
    if isinstance(value, (tuple, list)):
        return [stable(item) for item in value]
    return value


def differences(left, right, path="$", limit=20):
    result = []

    def visit(a, b, at):
        if len(result) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                visit(a.get(key), b.get(key), f"{at}.{key}")
        elif isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
            if len(a) != len(b):
                result.append(dict(path=at + ".length", first=len(a), second=len(b)))
            for index, (one, two) in enumerate(zip(a, b)):
                visit(one, two, f"{at}[{index}]")
        elif a != b:
            result.append(dict(path=at, first=str(a)[:120], second=str(b)[:120]))

    visit(left, right, path)
    return result


def logical_steps(steps):
    result = deepcopy(steps)
    for step in result:
        cursor = step["cursor"]
        seen = cursor["seen"]
        sequences = [int(identity.split(":")[1]) for identity in seen]
        cursor["seen"] = list(range(len(seen)))
        cursor["combat"] = cursor["combat"] - max(sequences) if sequences else None
    return result


def run(*, port=29920, device="cuda:0", seed=1755):
    started = time.monotonic()
    service = runtime()
    owned = service.load(ROOT / "engines" / str(port) / "process.json")
    if not owned_alive(service, owned, port) or not owned.get("ready"):
        raise RuntimeError("task-owned server engine is not ready")
    config = json.loads((ROOT / "config.json").read_text())
    plan = json.loads((ROOT / "match-plan.json").read_text())
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    policy = load_event_checkpoint((ROOT / config["start_checkpoint"]).resolve(), device=device)
    if policy.checkpoint_sha256 != config["start_checkpoint_sha256"]:
        raise ValueError("event acceptance checkpoint identity differs from 1755")
    native = NativeClashEnv(port=port, timeout=60)
    native._persistent_transport_requested = True
    saved = []
    adapter = None
    try:
        env = BattleEnvV1(native=native)
        rollout = EventPolicyRollout(env, {0: policy, 1: policy},
            reward=plan["source_training_state"]["reward_config"], match_id="event-acceptance")
        rollout.reset(decks={o: tuple(plan["learner_deck"]) for o in (0, 1)}, seed=seed)
        # Capture a real delayed command before it executes, with another owner
        # still ready. This exercises the Python queue, not just native restore.
        while not (rollout.ready and any(rollout.plans.values())):
            if rollout.tick > 1600:
                raise ValueError("policy did not produce a delayed plan for restoration acceptance")
            if rollout.ready:
                rollout.decide(rollout.ready[0], sample=True)
            else:
                rollout.step_native()
        branch = {"mode": None}
        last = {"steps": []}

        def capture():
            fair = {str(o): env.observe(owner=o, policy_features_only=True).to_dict() for o in (0, 1)}
            raw = stable(deepcopy(dict(env.raw_observation)))
            if raw.get("truncated") or any(obs["truncated"] for obs in fair.values()):
                raise ValueError("acceptance observation is truncated")
            return SearchState(native_snapshot={},
                observation={"tick": rollout.tick, "truncated": False, "native": raw, "fair": fair},
                actor=rollout.ready[0] if rollout.ready else 0,
                legal_actions=("policy", "wait"), external_queue=(),
                reward_ledger=rollout.reward_ledger(), event_cursor=rollout.event_cursor(),
                policy_state={"rollout": rollout.capture(), "branch": branch["mode"]})

        def restore(state):
            rollout.restore(state.policy_state["rollout"])
            branch["mode"] = state.policy_state["branch"]

        def apply(action):
            if action not in ("policy", "wait"):
                raise ValueError("unsupported event acceptance branch")
            branch["mode"] = action

        def advance(max_points, max_seconds):
            start_tick, decisions, before = rollout.tick, 0, rollout.totals[0]
            stop_tick = start_tick + int(max_seconds * 20)
            steps = []
            while decisions < max_points and rollout.tick < stop_tick:
                if rollout.terminal:
                    raise ValueError("short acceptance branch unexpectedly ended")
                if rollout.ready:
                    owner = rollout.ready[0]
                    item = dict(tick=rollout.tick, owner=owner,
                        input=tensor_record_sha256(rollout.frames[owner]),
                        mask=digest({name: tensor_record_sha256(tensor)
                                     for name, tensor in rollout.masks(owner).items()}))
                    step = rollout.decide(owner, key="wait" if branch["mode"] == "wait" else None,
                                          sample=True)
                    item.update(action=action_key(step.action), log_prob=float(step.log_prob[0]))
                    decisions += 1
                else:
                    outcomes = rollout.step_native(stop_tick=stop_tick)
                    item = dict(tick=rollout.tick, outcomes=[event.to_dict() for event in outcomes])
                item.update(native=digest(stable(dict(env.raw_observation))),
                    cursor=deepcopy(rollout.event_cursor()), rewards=digest(rollout.reward_ledger()))
                steps.append(item)
            last["steps"] = steps
            branch["mode"] = None
            return rollout.totals[0] - before, max(1, decisions), (rollout.tick - start_tick) / 20

        adapter = NativeSnapshotSearchAdapter(native, capture=capture, restore_sidecar=restore,
            apply=apply, advance=advance, battle_sidecar=BattleEnvSidecar(env))
        root = adapter.save_search_state()
        saved.append(root)
        root = SearchState.from_dict(json.loads(json.dumps(root.to_dict())))
        adapter.restore_search_state(root)
        fingerprint = adapter.fingerprint()
        branches, details, raw_step_hashes = [], [], []
        for action in ("policy", "wait", "policy"):
            adapter.restore_search_state(root)
            restored_sidecar = digest(rollout.capture())
            restored_native = digest(stable(dict(env.raw_observation)))
            adapter.apply_action(action)
            result = adapter.advance_until_decision(24, 10.)
            saved.append(result.state)
            branches.append(dict(action=action, tick=rollout.tick,
                restored_sidecar=restored_sidecar, restored_native=restored_native,
                state=state_digest(result.state), steps_sha256=digest(logical_steps(last["steps"])),
                reward=result.reward, decisions=result.decision_points,
                confirmed_actions=rollout.ledger.confirmed,
                rejected_actions=rollout.ledger.rejected, cancelled=rollout.cancellations))
            raw_step_hashes.append(digest(last["steps"]))
            details.append(deepcopy(last["steps"]))
        if branches[0] != branches[2] or logical_steps(details[0]) != logical_steps(details[2]):
            first_difference = next((dict(index=index, first=first, second=second)
                for index, (first, second) in enumerate(zip(details[0], details[2]))
                if first != second), None)
            raise ValueError("event-policy A/B/A differs: " + json.dumps(dict(
                branches=branches, raw_step_hashes=raw_step_hashes,
                first_difference=first_difference,
                step_differences=differences(details[0], details[2], limit=100),
                state_differences=differences(saved[1].to_dict(), saved[3].to_dict(), limit=100))))
        if branches[0]["state"] == branches[1]["state"] or not branches[0]["confirmed_actions"]:
            raise ValueError("acceptance needs distinct branches and real native execution receipts")
        if any(row["rejected_actions"] for row in branches):
            raise ValueError("native action rejection during event acceptance")
        adapter.restore_search_state(saved[1])
        nested = adapter.save_search_state()
        saved.append(nested)
        adapter.restore_search_state(root)
        adapter.restore_search_state(nested)
        restored = adapter.save_search_state()
        saved.append(restored)
        if state_digest(restored) != state_digest(nested):
            raise ValueError("nested event restore differs")
        adapter.restore_search_state(root)
        if adapter.fingerprint() != fingerprint:
            raise ValueError("event root fingerprint differs after restore")
        for state in saved:
            adapter.release_search_state(state)
        if adapter.owned_snapshot_count:
            raise ValueError("event acceptance leaked snapshot handles")
        return dict(schema="hog-event-policy-acceptance.v1", passed=True,
            scope="short real event-policy branches; full-game PUCT acceptance pending",
            seed=seed, checkpoint_sha256=policy.checkpoint_sha256, contract_sha256=policy.contract_sha256,
            device=device, port=port, owner_pid=owned["pid"], owner_start_ticks=owned["start_ticks"],
            pending_plan_at_root=True, branches=branches,
            raw_step_hashes=raw_step_hashes, json_restore=True,
            nested_restore=True, snapshot_handles_after=0, seconds=time.monotonic() - started)
    finally:
        if adapter is not None:
            for state in saved:
                adapter.release_search_state(state)
        native.close_transport()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=29920)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--seed", type=int, default=1755)
    result = run(**vars(parser.parse_args()))
    target = ROOT / "event-acceptance.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(target)
    print(json.dumps(result, indent=2), flush=True)
