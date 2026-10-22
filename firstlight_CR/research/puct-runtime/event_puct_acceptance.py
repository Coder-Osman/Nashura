"""Run a complete event PUCT teacher episode against a task-owned native engine."""
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
from runtime_contract import install, PROBE_SHA256

install()

from distillation import TeacherRecord, encode_model_inputs
from event_policy_rollout import EventPolicyRollout
from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from policy_signature import tensor_record_sha256
from server_engine import owned_alive, runtime
from tree_search import PuctSearch, SearchState, state_digest
from teacher_targets import finish_episode, reply_targets
from native_runner.battle_env import BattleEnvV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.training.event_checkpoint import load_event_checkpoint


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


def run(*, port=29920, device="cuda:0", seed=1755, simulations=32,
        episode_id="event-puct-acceptance",
        max_episode_seconds=3600.0, progress=None):
    started = time.monotonic()
    service = runtime()
    owned = service.load(ROOT / "engines" / str(port) / "process.json")
    if not owned_alive(service, owned, port) or not owned.get("ready"):
        raise RuntimeError("task-owned server engine is not ready")
    config = json.loads((ROOT / "config.json").read_text())
    plan = json.loads((ROOT / "match-plan.json").read_text())
    tree = plan["tree_search"]
    allowed = (tree["prototype_simulations"], tree["teacher_simulations"],
               tree["high_uncertainty_simulations"])
    if simulations not in allowed:
        raise ValueError("simulation count is outside the configured PUCT budgets")
    if not episode_id or "/" in episode_id or "\\" in episode_id:
        raise ValueError("episode id must be a nonempty filename-safe identifier")
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    policy = load_event_checkpoint(
        (ROOT / config["start_checkpoint"]).resolve(), device=device
    )
    if policy.checkpoint_sha256 != config["start_checkpoint_sha256"]:
        raise ValueError("event acceptance checkpoint identity differs from 1755")
    native = NativeClashEnv(port=port, timeout=60)
    native._persistent_transport_requested = True
    saved: list[SearchState] = []
    adapter = None
    try:
        env = BattleEnvV1(native=native)
        rollout = EventPolicyRollout(
            env, {0: policy, 1: policy},
            reward=plan["source_training_state"]["reward_config"],
            match_id=episode_id,
        )
        decks = {owner: tuple(plan["learner_deck"]) for owner in (0, 1)}
        rollout.reset(decks=decks, seed=seed)
        while not rollout.ready and not rollout.terminal:
            rollout.step_native()
        if rollout.terminal:
            raise ValueError("match ended before the first event decision")

        def capture() -> SearchState:
            fair = {
                str(owner): env.observe(owner=owner, policy_features_only=True).to_dict()
                for owner in (0, 1)
            }
            raw = stable(deepcopy(dict(env.raw_observation)))
            if raw.get("truncated") or any(item["truncated"] for item in fair.values()):
                raise ValueError("PUCT capture requires an untruncated observation")
            actor = rollout.ready[0] if rollout.ready else 0
            terminal = bool(rollout.terminal)
            legal = () if terminal else rollout.legal_action_keys(actor)
            return SearchState(
                native_snapshot={},
                observation={
                    "tick": rollout.tick, "truncated": False,
                    "native": raw, "fair": fair,
                },
                actor=actor,
                legal_actions=tuple(legal),
                external_queue=(),
                reward_ledger=rollout.reward_ledger(),
                event_cursor=rollout.event_cursor(),
                policy_state={"rollout": rollout.capture()},
                terminal=terminal,
            )

        def restore(state: SearchState) -> None:
            rollout.restore(state.policy_state["rollout"])

        def apply(action: str) -> None:
            if not rollout.ready:
                raise ValueError("PUCT action applied without an active event decision")
            owner = rollout.ready[0]
            if action not in rollout.legal_action_keys(owner):
                raise ValueError("PUCT selected an illegal event action")
            rollout.decide(owner, key=action, sample=False)

        def advance(max_points: int, max_seconds: float):
            if max_points < 1 or max_seconds <= 0:
                raise ValueError("invalid PUCT advance horizon")
            before = rollout.totals[rollout.learner_owner]
            start_tick = rollout.tick
            started_at = time.monotonic()
            while not rollout.terminal and not rollout.ready:
                if time.monotonic() - started_at >= max_seconds:
                    raise ValueError("native event transition exceeded search horizon")
                rollout.step_native()
            elapsed = time.monotonic() - started_at
            if elapsed > max_seconds + 1e-9:
                raise ValueError("native event transition exceeded search horizon")
            return (
                rollout.totals[rollout.learner_owner] - before,
                1,
                elapsed,
            )

        adapter = NativeSnapshotSearchAdapter(
            native,
            capture=capture,
            restore_sidecar=restore,
            apply=apply,
            advance=advance,
            battle_sidecar=BattleEnvSidecar(env),
        )
        root = adapter.save_search_state()
        saved.append(root)
        root_fingerprint = adapter.fingerprint()
        root_actor = root.actor
        semantic_frame, action_masks = encode_model_inputs(
            rollout.frames[root_actor], rollout.masks(root_actor))

        def prior(state: SearchState):
            return rollout.policy_prior(state.actor)

        def evaluate(state: SearchState) -> float:
            value = rollout.value_estimate(state.actor)
            return value if state.actor == root_actor else -value

        search = PuctSearch(
            adapter,
            prior,
            prior,
            evaluate,
            cpuct=tree["cpuct"],
            gamma=config["optimizer"]["gamma"],
            max_decision_points=tree["max_decision_points"],
            max_native_seconds=tree["max_native_seconds"],
            root_noise=simulations != tree["prototype_simulations"],
            opponent_top_k=tree["opponent_top_k"],
            opponent_temperature=tree["opponent_temperature"],
            seed=seed,
        )
        root_node = search.run(simulations, root_state=root)
        selected = max(root_node.state.legal_actions,
                       key=lambda action: (root_node.visits.get(action, 0), action))
        teacher_policy = root_node.policy(temperature=1.0)
        value_evidence = search.evaluate_root_reply_details(root_node, selected)
        q_expected, q_robust, q_target = reply_targets(value_evidence)
        after_fingerprint = adapter.fingerprint()
        if after_fingerprint != root_fingerprint:
            raise ValueError("PUCT did not restore the root native state")
        search_seconds = time.monotonic() - started
        search.release(root_node)
        adapter.release_search_state(root)
        saved.clear()
        if adapter.owned_snapshot_count:
            raise ValueError("PUCT acceptance leaked snapshot handles")
        settlement = finish_episode(rollout, first_action=selected,
                                    max_seconds=max_episode_seconds, progress=progress)
        record = TeacherRecord(
            episode_id=episode_id,
            decision_index=0,
            observation=root.observation,
            legal_actions=root.legal_actions,
            visit_counts={action: root_node.visits.get(action, 0)
                          for action in root.legal_actions},
            teacher_policy=teacher_policy,
            q_expected=q_expected,
            q_robust=q_robust,
            q_target=q_target,
            terminal_result=settlement["result_by_owner"][root_actor],
            opponent_sha256=policy.checkpoint_sha256,
            search_simulations=simulations,
            teacher_temperature=1.0,
            opponent_temperature=tree["opponent_temperature"],
            state_mode=tree["state_mode"],
            seed=seed,
            native_runtime_sha256=PROBE_SHA256,
            content_pack_sha256=digest(plan["learner_deck_identity"]),
            semantic_frame=semantic_frame,
            action_masks=action_masks,
            value_evidence=value_evidence,
            terminal_evidence=settlement,
        )
        record.require_training_ready()
        execution = rollout.ledger.counters()
        if execution["rejected"] != 0 or execution["unconfirmed"] != 0:
            raise ValueError(f"PUCT execution ledger is not clean: {execution}")
        if any(value for value in root_node.visits.values()
               if not isinstance(value, int) or value < 0):
            raise ValueError("PUCT visit counts are malformed")
        return {
            "schema": "hog-event-puct-acceptance.v1",
            "passed": True,
            "checkpoint_sha256": policy.checkpoint_sha256,
            "contract_sha256": policy.contract_sha256,
            "device": device,
            "port": port,
            "episode_id": episode_id,
            "seed": seed,
            "simulations": simulations,
            "root_tick": root.observation["tick"],
            "root_actor": root.actor,
            "root_legal_action_count": len(root.legal_actions),
            "selected_action": selected,
            "teacher_record": record.to_dict(),
            "root_fingerprint": root_fingerprint,
            "restored_root_fingerprint": after_fingerprint,
            "search_seconds": search_seconds,
            "episode": settlement,
            "execution": execution,
            "rollout": {
                "cancellations": rollout.cancellations,
                "decisions": rollout.decisions,
                "steps": rollout.steps,
                "native_events": rollout.native_events,
            },
            "snapshot_handles_after": adapter.owned_snapshot_count,
            "native_seconds": time.monotonic() - started,
            "native_runtime_probe_sha256": digest(stable(native.observe())),
        }
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
    parser.add_argument("--simulations", type=int, default=32)
    parser.add_argument("--episode-id", default="event-puct-acceptance")
    parser.add_argument("--output", type=Path, default=ROOT / "event-puct-acceptance.json")
    parser.add_argument("--max-episode-seconds", type=float, default=3600.0)
    args = parser.parse_args()
    target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        parser.error("output already exists")
    def progress(value):
        progress_path = target.with_suffix(".progress.json")
        temporary = progress_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(progress_path)
        print(json.dumps(value, allow_nan=False), flush=True)
    progress({"phase": "search", "simulations": args.simulations, "seed": args.seed})
    kwargs = vars(args).copy()
    kwargs.pop("output")
    result = run(**kwargs, progress=progress)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(target)
    print(json.dumps(result, indent=2), flush=True)
