"""Compare a full-game PUCT policy with the same checkpoint without search."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
if ROOT.name == "candidate":
    ROOT = ROOT.parents[1]
for import_path in (str(ROOT), str(ROOT.parents[1]), str(ROOT / "runtime")):
    while import_path in sys.path:
        sys.path.remove(import_path)
sys.path[:0] = [str(ROOT), str(ROOT.parents[1]), str(ROOT / "runtime")]

os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
import torch

from runtime_contract import install

install()

import packed_action as _search_packed_action

if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from event_policy_rollout import EventPolicyRollout
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))
from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from native_runner.battle_env import BattleEnvV1
from native_runner.cr_native_env import NativeClashEnv
from native_runner.training.v4.factory import build_resident_training_proxies_v4
from native_runner.training.event_checkpoint import load_event_checkpoint
from resident_search_proxy import SingleSlotResidentSearchProxy
from server_engine import owned_alive, runtime
from teacher_targets import terminal_evidence
from tree_search import PuctSearch, SearchState, SearchView, state_digest
from action_codec import action_key
from native_runner.training.event_cache import canonicalize_event_observation
from native_runner.training.event_ppo import _stack_frames
from policy_rollout import _restore_tensorizer, _tensorizer_snapshot



def _packed_action_space_digest(action_space) -> str:
    digest = hashlib.blake2b(digest_size=32)
    for name in (
            "kind", "card_row", "cell", "delay", "ability_source",
            "canonical_tie_rank"):
        value = getattr(action_space, name)
        if hasattr(value, "detach"):
            value = value.detach().cpu().contiguous()
            digest.update(str(value.dtype).encode())
            digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
            digest.update(value.numpy().tobytes())
        else:
            digest.update(repr(value).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _compact_evaluation_state_key(state) -> str:
    native_identity = getattr(state, "execution_digest", None)
    if native_identity is None:
        native_identity = state.native_snapshot.get("state_sha256")
    if not native_identity:
        raise ValueError("compact state key requires a native execution identity")
    action_space = getattr(state, "action_space", None)
    if action_space is not None:
        cached_digest = getattr(action_space, "semantic_sha256", None)
        actions = {"packed": (cached_digest if isinstance(cached_digest, str)
                                else _packed_action_space_digest(action_space))}
    else:
        actions = {"legal": tuple(state.legal_actions)}
    payload = {
        "version": "evaluation-state-key.compact.v1",
        "native": native_identity,
        "actor": state.actor,
        "terminal": state.terminal,
        "tick": state.observation.get("tick"),
        "actions": actions,
        "reward": state.reward_ledger,
        "external_queue": state.external_queue,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _view_actor(rollout: EventPolicyRollout) -> int:
    """Return a valid view actor even after the rollout reaches terminal state."""
    return rollout.ready[0] if rollout.ready else rollout.learner_owner


def run_match(*, port: int, device: str, seed: int, puct_owner: int,
              simulations: int, max_seconds: float, transport: str = "direct",
              resident_capture: str = "json", first_root_only: bool = False,
              profile_setup: bool = False, policy_pool=None) -> dict:
    if transport not in {"direct", "resident", "resident-hybrid"} or (
            transport == "resident" and simulations):
        raise ValueError("resident search requires the explicit resident-hybrid transport")
    if transport == "resident-hybrid" and simulations == 0:
        raise ValueError("resident-hybrid transport requires simulations")
    if first_root_only and simulations == 0:
        raise ValueError("first-root probe requires simulations")
    if resident_capture not in {"json", "zlib-json", "compact"}:
        raise ValueError("unsupported resident capture mode")
    started = time.monotonic()
    state_key_mode = os.environ.get("PUCT_STATE_KEY_MODE", "compact")
    if state_key_mode not in {"compact", "legacy"}:
        raise ValueError("PUCT_STATE_KEY_MODE must be compact or legacy")
    full_view_observation = (
        state_key_mode == "legacy"
        or os.environ.get("PUCT_FULL_VIEW_OBSERVATION", "0") == "1"
    )
    verify_restore_digest = os.environ.get("PUCT_VERIFY_RESTORE_DIGEST", "1") != "0"
    config = json.loads((ROOT / "config.json").read_text())
    plan = json.loads((ROOT / "match-plan.json").read_text())
    service = runtime()
    engine = service.load(ROOT / "engines" / str(port) / "process.json")
    allow_shared_engine = os.environ.get("PUCT_ALLOW_SHARED_PROFILE_ENGINE", "0") == "1"
    if (not engine.get("ready") or
            (not owned_alive(service, engine, port) and not allow_shared_engine)):
        raise RuntimeError(f"task-owned engine {port} is not ready")
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    checkpoint = (ROOT / config["start_checkpoint"]).resolve()
    if policy_pool is None:
        policy = load_event_checkpoint(checkpoint, device=device)
    else:
        policy = policy_pool.load(checkpoint, device=device,
                                  expected_sha256=config["start_checkpoint_sha256"])
    if policy.checkpoint_sha256 != config["start_checkpoint_sha256"]:
        raise ValueError("evaluation checkpoint identity differs from 1755")
    coordinator = None
    persistent_resident_search = (
        transport == "resident-hybrid"
        and os.environ.get("PUCT_RESIDENT_SEARCH_PERSISTENT", "0") == "1"
    )
    compact_resident_search = (
        transport == "resident-hybrid"
        and os.environ.get("PUCT_RESIDENT_SEARCH_COMPACT_CAPTURE", "1") != "0"
    )
    if transport in {"resident", "resident-hybrid"}:
        (native,), coordinator = build_resident_training_proxies_v4(
            (0,), port=port, timeout=90., capture_mode=resident_capture,
            step_until_event=False)
        if transport == "resident-hybrid":
            native = SingleSlotResidentSearchProxy(native, coordinator)
            native._compact_step_requested = compact_resident_search
    else:
        native = NativeClashEnv(port=port, timeout=60)
        native._persistent_transport_requested = True
    rollout = None
    search_decisions = 0
    search_seconds = 0.0
    search_setup_seconds = 0.0
    setup_spans_seconds: dict[str, float] = {}
    adapter_spans_ms: dict[str, float] = {}
    non_search_decide_seconds = 0.0
    step_wall_seconds = {"search": 0.0, "non_search": 0.0}
    step_calls = {"search": 0, "non_search": 0}
    step_profiles = {"search": {}, "non_search": {}}
    action_trace = hashlib.sha256()
    tick_trace = hashlib.sha256()
    search_active = [False]
    packed_search = simulations > 0
    leaf_batch_size = int(os.environ.get("PUCT_LEAF_BATCH_SIZE", "1"))
    if leaf_batch_size < 1:
        raise ValueError("PUCT_LEAF_BATCH_SIZE must be positive")
    try:
        env = BattleEnvV1(native=native)
        rollout = EventPolicyRollout(
            env, {0: policy, 1: policy},
            reward=plan["source_training_state"]["reward_config"],
            match_id=f"puct-vs-policy-{seed}-{puct_owner}",
        )
        rollout.step_timing_profile = step_profiles["non_search"]
        decks = {owner: tuple(plan["learner_deck"]) for owner in (0, 1)}
        rollout.reset(decks=decks, seed=seed)
        if coordinator is not None:
            coordinator.start_session()

        def step_native() -> None:
            scope = "search" if search_active[0] else "non_search"
            begun = time.monotonic()
            try:
                if coordinator is not None and coordinator.session_active:
                    coordinator.begin_round((0,))
                    try:
                        rollout.step_native()
                    except BaseException:
                        coordinator.abort_round(RuntimeError("evaluation native step failed"))
                        raise
                    else:
                        coordinator.finish_round()
                else:
                    rollout.step_native()
                if not search_active[0]:
                    tick_trace.update(f"{rollout.tick}:{int(rollout.terminal)}\n".encode())
            finally:
                step_wall_seconds[scope] += time.monotonic() - begun
                step_calls[scope] += 1

        while not rollout.ready and not rollout.terminal:
            step_native()

        def search_event_cursor() -> dict:
            epoch = str(env.raw_observation.get("stateEpoch"))
            seen = sorted(
                identity for identity in rollout.seen_events
                if identity.split(":", 1)[0] == epoch
            )
            return {
                "seen": seen,
                "combat": env._combat_next_sequence,
                "public_card_play": env._public_card_play_next_sequence,
            }

        def capture() -> SearchState:
            actor = _view_actor(rollout)
            fair = {
                str(owner): env.observe(owner=owner, policy_features_only=True).to_dict()
                for owner in (0, 1)
            }
            raw = dict(env.raw_observation)
            if raw.get("truncated") or any(item["truncated"] for item in fair.values()):
                raise ValueError("PUCT evaluation requires untruncated observations")
            action_space = (rollout.packed_action_space(actor)
                            if packed_search and not rollout.terminal else None)
            legal_actions = (() if rollout.terminal or packed_search else
                             tuple(rollout.legal_action_keys(actor)))
            return SearchState(
                native_snapshot={},
                observation={"tick": rollout.tick, "truncated": False, "native": raw, "fair": fair},
                actor=actor,
                legal_actions=legal_actions,
                external_queue=(),
                reward_ledger=rollout.search_reward_ledger(),
                event_cursor=rollout.event_cursor(),
                policy_state=(
                    {"rollout": rollout.capture_search()}
                    if packed_search else {}
                ),
                terminal=rollout.terminal,
                action_space=action_space,
            )

        def capture_view() -> SearchView:
            actor = _view_actor(rollout)
            if full_view_observation:
                observations = rollout.current_policy_observations()
                fair = {str(owner): observations[owner].to_dict() for owner in (0, 1)}
            else:
                fair = {}
            action_space = (rollout.packed_action_space(actor)
                            if packed_search and not rollout.terminal else None)
            legal_actions = (() if rollout.terminal or packed_search else
                             tuple(rollout.legal_action_keys(actor)))
            return SearchView(
                observation={"tick": rollout.tick, "truncated": False, "fair": fair},
                actor=actor,
                legal_actions=legal_actions,
                external_queue=(), reward_ledger=rollout.search_reward_ledger(),
                event_cursor=rollout.event_cursor(),
                policy_state={},
                terminal=rollout.terminal,
                action_space=action_space,
            )

        def capture_checkpoint() -> SearchState:
            """Build the small search view paired with the edge capsule."""
            view = capture_view()
            return SearchState(
                native_snapshot={}, observation=view.observation,
                actor=view.actor, legal_actions=view.legal_actions,
                external_queue=view.external_queue,
                reward_ledger=view.reward_ledger, event_cursor=view.event_cursor,
                policy_state={}, terminal=view.terminal,
                action_space=view.action_space,
            )

        def evaluation_state_key(state) -> str:
            if state_key_mode == "compact":
                return _compact_evaluation_state_key(state)
            stable_cursor = {
                "seen": [], "combat": 0, "public_card_play": None, "native": {},
            }
            return state_digest(replace(
                state, event_cursor=stable_cursor, policy_state={}
            ))

        def restore(state: SearchState) -> None:
            rollout.restore_search(state.policy_state["rollout"])

        def apply(action) -> None:
            actor = rollout.ready[0]
            key = (rollout.decode_packed_action(actor, action)
                   if packed_search else action)
            if not packed_search and key not in rollout.legal_action_keys(actor):
                raise ValueError("PUCT selected an illegal action")
            if search_active[0]:
                rollout.commit_search_action(actor, key)
            else:
                rollout.decide(actor, key=key, sample=False)

        root_actor = [puct_owner]

        def advance(max_points: int, max_native_seconds: float):
            before = rollout.totals[root_actor[0]]
            begin = time.monotonic()
            start_tick = rollout.tick
            while not rollout.terminal and not rollout.ready:
                if time.monotonic() - begin >= max_native_seconds:
                    raise ValueError("native transition exceeded search horizon")
                step_native()
            elapsed = time.monotonic() - begin
            if elapsed > max_native_seconds + 1e-9:
                raise ValueError("native transition exceeded search horizon")
            return rollout.totals[root_actor[0]] - before, 1, elapsed

        def capture_leaf(state):
            """Tensorize a leaf while its restored native state is live."""
            owner = int(state.actor)
            observations = rollout.current_policy_observations()
            tensorizer = rollout.tensorizers[owner]
            saved = _tensorizer_snapshot(tensorizer)
            try:
                frame = canonicalize_event_observation(
                    tensorizer.tensorize(observations[owner], validate=False)
                )
            finally:
                _restore_tensorizer(tensorizer, saved)
            return {"frame": frame, "owner": owner}

        def evaluate_batch(items):
            if not items:
                return []
            if any(not isinstance(item, dict) or "frame" not in item for item in items):
                raise ValueError("batch leaf item is missing a captured frame")
            frames = _stack_frames(tuple(item["frame"] for item in items))
            with torch.inference_mode():
                context = policy.model.encode(
                    frames.to_model_input(device, float_dtype=torch.float32),
                    policy.model.initial_state(len(items), device=device),
                    episode_start=torch.ones(
                        len(items), dtype=torch.bool, device=device
                    ),
                )
                values = context.value.reshape(-1).detach().float().cpu().tolist()
            return [
                float(value) if item["owner"] == root_actor[0] else -float(value)
                for value, item in zip(values, items)
            ]

        while not rollout.terminal:
            if time.monotonic() - started >= max_seconds:
                raise TimeoutError("evaluation match deadline reached")
            if not rollout.ready:
                step_native()
                continue
            actor = rollout.ready[0]
            if actor != puct_owner:
                begun = time.monotonic()
                step = rollout.decide(actor, sample=True)
                action_trace.update(f"{rollout.tick}:{actor}:{action_key(step.action)}\n".encode())
                non_search_decide_seconds += time.monotonic() - begun
                continue
            if simulations == 0:
                begun = time.monotonic()
                step = rollout.decide(actor, sample=True)
                action_trace.update(f"{rollout.tick}:{actor}:{action_key(step.action)}\n".encode())
                non_search_decide_seconds += time.monotonic() - begun
                continue

            checkpoint = None
            root_node = None
            root_probe = None
            setup_started = time.monotonic()
            if transport == "resident-hybrid":
                coordinator.close_session()
                if persistent_resident_search:
                    native.native._persistent_transport_requested = True
                if profile_setup:
                    setup_spans_seconds["close_session"] = (
                        setup_spans_seconds.get("close_session", 0.0) + time.monotonic() - setup_started)
            battle_sidecar = BattleEnvSidecar(
                env, discard_epoch_cache=transport == "resident-hybrid",
                share_immutable_events=transport == "resident-hybrid",
                copy_on_write_mappings=transport == "resident-hybrid",
            )
            adapter = NativeSnapshotSearchAdapter(
                native, capture=capture, restore_sidecar=restore, apply=apply,
                advance=advance, battle_sidecar=battle_sidecar,
                capture_view=capture_view, copy_capture=False, validate_state=False,
                capture_root_capsule=(rollout.capture_root_capsule if packed_search else None),
                restore_root_capsule=(rollout.restore_root_capsule if packed_search else None),
                capture_checkpoint=(capture_checkpoint if packed_search else None),
                capture_checkpoint_capsule=(rollout.capture_search_checkpoint
                                           if packed_search else None),
                restore_checkpoint_capsule=(rollout.restore_search_checkpoint
                                            if packed_search else None),
                verify_restore_digest=verify_restore_digest,
                fast_restore_atomic=(
                    simulations > 0
                    and os.environ.get("PUCT_FAST_RESTORE_ATOMIC", "1") == "1"
                ),
                profile_enabled=profile_setup,
            )
            battle_sidecar.profile_sink = adapter._add_profile if profile_setup else None
            rollout.profile_sink = adapter._add_profile if profile_setup else None
            try:
                span_started = time.monotonic()
                rollout.begin_search_root(search_only=True)
                if profile_setup:
                    setup_spans_seconds["begin_search_root"] = (
                        setup_spans_seconds.get("begin_search_root", 0.0) + time.monotonic() - span_started)
                search_active[0] = True
                rollout.step_timing_profile = step_profiles["search"]
                span_started = time.monotonic()
                checkpoint = adapter.checkpoint_restorable()
                if profile_setup:
                    setup_spans_seconds["checkpoint"] = (
                        setup_spans_seconds.get("checkpoint", 0.0) + time.monotonic() - span_started)
                view = adapter.view_from_checkpoint(checkpoint)
                root_fingerprint = adapter.fingerprint() if first_root_only else None
                root_actor[0] = actor
                span_started = time.monotonic()
                search = PuctSearch(
                    adapter,
                    lambda state: (rollout.packed_policy_prior(state.actor, space=state.action_space)
                                   if packed_search else rollout.policy_prior(state.actor)),
                    lambda state: (rollout.packed_policy_prior(state.actor, space=state.action_space)
                                   if packed_search else rollout.policy_prior(state.actor)),
                    lambda state: rollout.value_estimate(state.actor)
                    if state.actor == actor else -rollout.value_estimate(state.actor),
                    cpuct=plan["tree_search"]["cpuct"],
                    gamma=config["optimizer"]["gamma"],
                    max_decision_points=plan["tree_search"]["max_decision_points"],
                    max_native_seconds=float(os.environ.get(
                        "PUCT_SEARCH_MAX_NATIVE_SECONDS",
                        plan["tree_search"]["max_native_seconds"])),
                    root_noise=False,
                    opponent_top_k=plan["tree_search"]["opponent_top_k"],
                    opponent_temperature=plan["tree_search"]["opponent_temperature"],
                    seed=seed + search_decisions,
                    compact_children=True,
                    state_key=evaluation_state_key,
                    edge_memo=True,
                    edge_verify_every=int(os.environ.get("PUCT_EDGE_VERIFY_EVERY", "0")),
                    edge_checkpoints=os.environ.get("PUCT_EDGE_CHECKPOINTS", "0") == "1",
                    edge_checkpoint_min_replays=int(
                        os.environ.get("PUCT_EDGE_CHECKPOINT_MIN_REPLAYS", "3")),
                    evaluate_batch=(evaluate_batch if leaf_batch_size > 1 else None),
                    capture_leaf=(capture_leaf if leaf_batch_size > 1 else None),
                    leaf_batch_size=leaf_batch_size,
                )
                if profile_setup:
                    setup_spans_seconds["search_construct"] = (
                        setup_spans_seconds.get("search_construct", 0.0) + time.monotonic() - span_started)
                search_setup_seconds += time.monotonic() - setup_started
                search_started = time.monotonic()
                root_node = search.run(simulations, root_state=view,
                                       root_checkpoint=checkpoint)
                selected_token = max(
                    root_node.visits,
                    key=lambda action: (
                        root_node.visits[action],
                        (-root_node.action_space.canonical_tie_rank[action]
                         if root_node.action_space is not None else action),
                    ),
                )
                selected = (root_node.action_space.decode_one(selected_token)
                            if root_node.action_space is not None else selected_token)
                if first_root_only:
                    decode = (root_node.action_space.decode_one if root_node.action_space is not None
                              else lambda action: action)
                    root_probe = {
                        "root_tick": view.observation["tick"],
                        "native_root_sha256": checkpoint.state.native_snapshot["state_sha256"],
                        "selected_action": selected,
                        "visit_counts": {decode(action): count for action, count in root_node.visits.items()
                                         if count},
                        "q_values": {decode(action): root_node.q(action)
                                     for action, count in root_node.visits.items() if count},
                        "root_fingerprint": root_fingerprint,
                    }
                adapter.restore(checkpoint)
                if first_root_only:
                    root_probe["fingerprint_match"] = adapter.fingerprint() == root_fingerprint
                search_active[0] = False
                rollout.step_timing_profile = step_profiles["non_search"]
                rollout.end_search_root()
                rollout.decide(actor, key=selected, sample=False)
                action_trace.update(f"{rollout.tick}:{actor}:{selected}\n".encode())
                search_seconds += time.monotonic() - search_started
                search_decisions += 1
            finally:
                search_active[0] = False
                rollout.step_timing_profile = step_profiles["non_search"]
                rollout.profile_sink = None
                if root_node is not None:
                    search.release(root_node)
                if checkpoint is not None:
                    adapter.release(checkpoint)
                if (rollout._search_recorder_capsule is not None or
                        rollout._search_execution_ledger is not None):
                    rollout.end_search_root()
                if adapter.owned_snapshot_count:
                    raise ValueError("PUCT evaluation leaked snapshot handles")
                if profile_setup:
                    for name, milliseconds in adapter.profile.items():
                        adapter_spans_ms[name] = adapter_spans_ms.get(name, 0.0) + milliseconds
            if first_root_only:
                if root_probe is None:
                    raise ValueError("first-root search produced no evidence")
                status = native.native._raw_request("status") if coordinator is not None else native.status()
                root_probe.update(
                    schema="hog-puct-first-root-probe.v1", port=port, seed=seed,
                    transport=transport, simulations=simulations,
                    state_key_mode=state_key_mode,
                    verify_restore_digest=verify_restore_digest,
                    leaf_batch_size=leaf_batch_size,
                    checkpoint_sha256=policy.checkpoint_sha256,
                    search_seconds=search_seconds,
                    setup_seconds=search_setup_seconds,
                    snapshot_handles_after=status.get("snapshotHandles"),
                    resident_channel=coordinator.profile() if coordinator is not None else None,
                    setup_spans_seconds=setup_spans_seconds if profile_setup else None,
                    adapter_spans_ms=adapter_spans_ms if profile_setup else None,
                    profile_counters=(dict(sorted(rollout.profile_counters.items()))
                                      if profile_setup else None),
                )
                return root_probe
            if transport == "resident-hybrid":
                if persistent_resident_search:
                    native.native.close_transport()
                coordinator.start_session()

        if coordinator is not None:
            coordinator.close_session()
            digest_receipt = native.native._raw_request("state-digest")
            if digest_receipt.get("tick") != rollout.tick:
                raise ValueError("resident terminal digest tick differs from the settled slot")
            evidence = terminal_evidence(
                rollout, native_state_sha256=digest_receipt.get("sha256"))
        else:
            evidence = terminal_evidence(rollout)
        return {
            "schema": "hog-puct-vs-policy-eval.v1",
            "passed": True,
            "seed": seed,
            "port": port,
            "device": device,
            "transport": transport,
            "resident_capture": resident_capture if coordinator is not None else None,
            "puct_owner": puct_owner,
            "simulations": simulations,
            "state_key_mode": state_key_mode,
            "verify_restore_digest": verify_restore_digest,
            "leaf_batch_size": leaf_batch_size,
            "checkpoint_sha256": policy.checkpoint_sha256,
            "result_by_owner": evidence["result_by_owner"],
            "winner": evidence["winner"],
            "terminal_tick": evidence["terminal_tick"],
            "decisions": rollout.decisions,
            "puct_decisions": search_decisions,
            "puct_search_seconds": search_seconds,
            "elapsed_seconds": time.monotonic() - started,
            "performance": {
                "action_trace_sha256": action_trace.hexdigest(),
                "tick_trace_sha256": tick_trace.hexdigest(),
                "step_wall_seconds": step_wall_seconds,
                "step_calls": step_calls,
                "battle_step_seconds": step_profiles,
                "non_search_decide_seconds": non_search_decide_seconds,
                "search_setup_seconds": search_setup_seconds,
                "state_key_mode": state_key_mode,
                "setup_spans_seconds": setup_spans_seconds if profile_setup else None,
                "adapter_spans_ms": adapter_spans_ms if profile_setup else None,
                "resident_channel": coordinator.profile() if coordinator is not None else None,
            },
            "execution": rollout.ledger.counters(),
        }
    finally:
        try:
            if coordinator is not None:
                coordinator.close_session()
                native.native._raw_request("multi-stop")
        finally:
            native.close_transport()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--device", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--puct-owner", required=True, type=int, choices=(0, 1))
    parser.add_argument("--simulations", type=int, choices=range(0, 257), default=32)
    parser.add_argument("--transport", choices=("direct", "resident", "resident-hybrid"), default="direct")
    parser.add_argument("--resident-capture", choices=("json", "zlib-json", "compact"),
                        default="json")
    parser.add_argument("--max-seconds", type=float, default=7200.)
    parser.add_argument("--first-root-only", action="store_true")
    parser.add_argument("--profile-setup", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    options = vars(args).copy()
    options.pop("output")
    result = run_match(**options)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    temporary.replace(args.output)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
