from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import random
import statistics
import sys
import time
from dataclasses import fields, replace
from pathlib import Path
from types import MethodType

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

ROOT = Path("research/puct").resolve()
P10_ROOT = Path(os.environ.get("PUCT_CANDIDATE_ROOT", ROOT)).resolve()
PROJECT = Path.cwd()
sys.path[:0] = [str(ROOT), str(ROOT / "runtime"), str(PROJECT), str(P10_ROOT)]

import torch

from runtime_contract import PROBE_SHA256, install

install()
from server_engine import owned_alive, runtime

sys.path.insert(0, str(P10_ROOT))
import event_policy_rollout as event_rollout_module
EventPolicyRollout = event_rollout_module.EventPolicyRollout
if str(P10_ROOT) in sys.path:
    sys.path.remove(str(P10_ROOT))
sys.path.insert(0, str(P10_ROOT))
import native_adapter as search_adapter_module
import tree_search as search_tree_module
if (Path(event_rollout_module.__file__).resolve().parent != P10_ROOT
        or Path(search_adapter_module.__file__).resolve().parent != P10_ROOT
        or Path(search_tree_module.__file__).resolve().parent != P10_ROOT):
    raise RuntimeError("profile probe resolved a non-v3 PUCT module")
from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from native_runner.battle_env import BattleEnvV1
from native_runner.cr_native_env import ResidentNativeClashEnv
from native_runner.training.event_cache import canonicalize_event_observation
from native_runner.training.event_ppo import _stack_frames
from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.event_tower_rewards import (
    tower_config,
    tower_potential,
    tower_state,
    validate_reward_gamma,
)
from policy_rollout import _restore_tensorizer, _tensorizer_snapshot
from research.firstlight_v2.search import LearnerDecisionDiscount, unshape_bootstrap
from tree_search import PuctSearch, SearchState, SearchView, state_digest
from packed_action import PackedActionSpace


PORT = int(os.environ.get("PUCT_PROFILE_PORT", "29942"))
ALLOW_SHARED_PROFILE_ENGINE = os.environ.get("PUCT_ALLOW_SHARED_PROFILE_ENGINE", "0") == "1"
SEED = 320049
SIMULATIONS = 32
LEAF_BATCH_SIZE = int(os.environ.get("PUCT_LEAF_BATCH_SIZE", "1"))
BF16 = os.environ.get("PUCT_BF16", "0") == "1"
PROFILE_MODES = tuple(filter(None, os.environ.get(
    "PUCT_PROFILE_MODES", "baseline,candidate").split(",")))
PROFILE_TRIALS = int(os.environ.get("PUCT_PROFILE_TRIALS", "5"))
PACKED_PRIOR = os.environ.get("PUCT_P12_PACKED", "0") == "1"
ROOT_CAPSULE = os.environ.get("PUCT_P13_ROOT_CAPSULE", "0") == "1"
EPISODE_ID = os.environ.get("PUCT_PROFILE_EPISODE_ID", "p10-native-root-checkpoint-cache-20261003")
OUTPUT = Path(os.environ.get(
    "PUCT_PROFILE_OUTPUT", "research/puct/evidence/p11_search_only_ab_20261003.json"))
PARTIAL_OUTPUT = OUTPUT.with_suffix(".partial.json")


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"snapshotHandles", "stateEpoch"}}
    if isinstance(value, (list, tuple)):
        return [stable(item) for item in value]
    return value


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_normalize(item):
    if isinstance(item, PackedActionSpace):
        return {
            "packed_action_space": {
                "kind": audit_normalize(item.kind),
                "card_row": audit_normalize(item.card_row),
                "cell": audit_normalize(item.cell),
                "delay": audit_normalize(item.delay),
                "ability_source": audit_normalize(item.ability_source),
                "canonical_tie_rank": audit_normalize(item.canonical_tie_rank),
            }
        }
    if hasattr(item, "detach") and hasattr(item, "tolist") and hasattr(item, "shape"):
        tensor = item.detach().cpu().contiguous()
        raw = tensor.view(torch.uint8).numpy().tobytes()
        return {"dtype": str(tensor.dtype), "shape": list(tensor.shape),
                "sha256": __import__("hashlib").sha256(raw).hexdigest()}
    if hasattr(item, "tolist") and hasattr(item, "shape"):
        array = item
        raw = json.dumps(array.tolist(), separators=(",", ":"),
                         allow_nan=False).encode()
        return {"dtype": str(getattr(array, "dtype", "unknown")),
                "shape": list(array.shape),
                "sha256": __import__("hashlib").sha256(raw).hexdigest()}
    if hasattr(item, "semantic_identity"):
        return audit_normalize(item.semantic_identity())
    if isinstance(item, dict):
        if item.get("__type__") == "tensor" and "values" in item:
            raw = json.dumps(item["values"], sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
            return {"__type__": "tensor", "dtype": item.get("dtype"),
                    "shape": item.get("shape"),
                    "sha256": __import__("hashlib").sha256(raw).hexdigest()}
        return {str(key): audit_normalize(child) for key, child in sorted(
            item.items(), key=lambda pair: str(pair[0]))}
    if isinstance(item, (list, tuple)):
        if len(item) > 128:
            raw = json.dumps(item, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
            return {"__type__": "sequence", "length": len(item),
                    "sha256": __import__("hashlib").sha256(raw).hexdigest()}
        return [audit_normalize(child) for child in item]
    if isinstance(item, bytes):
        return {"bytes": len(item),
                "sha256": __import__("hashlib").sha256(item).hexdigest()}
    if item is None or isinstance(item, (bool, int, float, str)):
        return item
    raise TypeError(f"unsupported audit value type: {type(item).__name__}")


def snapshot_count(status):
    return int(status.get("snapshotHandles") or 0)

def audit_value_hash(value):
    encoded = json.dumps(audit_normalize(value), sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    return __import__("hashlib").sha256(encoded).hexdigest()


def field_hashes(state):
    result = {}
    def walk(path, value):
        if isinstance(value, dict) and value:
            for key, child in value.items():
                walk(f"{path}.{key}", child)
        else:
            result[path] = audit_value_hash(value)

    for field in fields(state):
        walk(field.name, getattr(state, field.name))
    return result


def component_hashes(state):
    return {field.name: audit_value_hash(getattr(state, field.name))
            for field in fields(state)}


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        raise RuntimeError(f"evidence already exists: {OUTPUT}")
    lock_path = ROOT / "engines" / str(PORT) / "collection.lock"
    lock = lock_path.open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    service = runtime()
    owned = service.load(ROOT / "engines" / str(PORT) / "process.json")
    if not owned.get("ready") or (not owned_alive(service, owned, PORT)
                                   and not ALLOW_SHARED_PROFILE_ENGINE):
        raise RuntimeError("task-owned engine is not ready")
    status = service.request(PORT, "status")
    if (status.get("coldReady") is not True or status.get("contentReady") is not True
            or snapshot_count(status) != 0 or status.get("paused") is not True):
        raise RuntimeError("profile engine must be ready, paused, and own zero snapshots")

    config = json.loads((ROOT / "config.json").read_text())
    plan = json.loads((ROOT / "match-plan.json").read_text())
    tree = plan["tree_search"]
    reward = plan["source_training_state"]["reward_config"]
    validate_reward_gamma(reward, config["optimizer"]["gamma"])
    shaping = tower_config(reward)
    if (tree["opponent_top_k"] != 4 or tree["opponent_temperature"] != 0.5
            or tree["state_mode"] != "deterministic_full_state"):
        raise RuntimeError("frozen search contract mismatch")

    torch.set_num_threads(1)
    torch.manual_seed(SEED)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    policy = load_event_checkpoint(Path("1755.pt").resolve(), device="cuda:0")
    expected_checkpoint = "e9f7c84a3e5f84a4d625c3f9a68e332a318a2aedc06e196da55bdb00a66bd1f0"
    if policy.checkpoint_sha256 != expected_checkpoint:
        raise RuntimeError("checkpoint identity mismatch")
    if os.environ.get("PUCT_STATIC_PROFILE_CACHE", "0") == "1":
        backbone = policy.model.backbone
        with torch.inference_mode():
            cached_effect_memory = backbone.effect_encoder.all_effects()
            cached_profile_memory = backbone.mechanic_profile_encoder(cached_effect_memory)

        def cached_all_effects(_self):
            return cached_effect_memory

        def cached_profile_forward(_self, _effect_memory):
            return cached_profile_memory

        backbone.effect_encoder.all_effects = MethodType(
            cached_all_effects, backbone.effect_encoder
        )
        backbone.mechanic_profile_encoder.forward = MethodType(
            cached_profile_forward, backbone.mechanic_profile_encoder
        )
    if os.environ.get("PUCT_TORCH_COMPILE", "0") == "1":
        compiled_encode = torch.compile(
            policy.model.encode, mode="reduce-overhead", fullgraph=False
        )
        policy.model.encode = compiled_encode

    native = ResidentNativeClashEnv(env_id=int(os.environ.get("PUCT_PROFILE_ENV_ID", "1")), port=PORT, timeout=60)
    native._persistent_transport_requested = True
    env = BattleEnvV1(native=native)
    rollout = EventPolicyRollout(
        env, {0: policy, 1: policy}, reward=reward,
        match_id=EPISODE_ID, learner_owner=0,
    )
    decks = {owner: tuple(plan["learner_deck"]) for owner in (0, 1)}
    active_mode = "baseline"

    def fast_path():
        return active_mode in {"candidate", "candidate-fast", "candidate-cache", "candidate-view",
                               "candidate-cache-view"}

    def capture():
        fair = {
            str(owner): env.observe(owner=owner, policy_features_only=True).to_dict()
            for owner in (0, 1)
        }
        actor = rollout.ready[0] if rollout.ready else 0
        terminal = bool(rollout.terminal)
        action_space = (None if terminal or not PACKED_PRIOR
                        else rollout.packed_action_space(actor))
        legal = (() if terminal or action_space is not None
                 else rollout.legal_action_keys(actor))
        return SearchState(
            native_snapshot={},
            observation={"tick": rollout.tick, "truncated": False,
                         "native": stable(dict(env.raw_observation)), "fair": fair},
            actor=actor, legal_actions=tuple(legal), external_queue=(),
            reward_ledger=rollout.search_reward_ledger(),
            event_cursor=rollout.event_cursor(),
            policy_state=({} if ROOT_CAPSULE and fast_path()
                          else {"rollout": rollout.capture_search()}),
            terminal=terminal,
            action_space=action_space,
        )

    def capture_view():
        observations = rollout.current_policy_observations()
        fair = {str(owner): observations[owner].to_dict() for owner in (0, 1)}
        actor = rollout.ready[0] if rollout.ready else 0
        terminal = bool(rollout.terminal)
        action_space = (None if terminal or not PACKED_PRIOR
                        else rollout.packed_action_space(actor))
        legal = (() if terminal or action_space is not None
                 else rollout.legal_action_keys(actor))
        return SearchView(
            observation={"tick": rollout.tick, "truncated": False, "fair": fair},
            actor=actor, legal_actions=tuple(legal), external_queue=(),
            reward_ledger=rollout.search_reward_ledger(),
            event_cursor=rollout.event_cursor(),
            policy_state={"rollout_semantic_digest": rollout.search_view_digest()},
            terminal=terminal, action_space=action_space,
        )

    def apply(action):
        owner = rollout.ready[0]
        if PACKED_PRIOR:
            action = (rollout.decode_packed_action(owner, action) if fast_path()
                      else rollout.packed_action_space(owner).decode_one(action))
        elif action not in rollout.legal_action_keys(owner):
            raise ValueError("illegal PUCT action")
        if active_mode != "baseline":
            rollout.commit_search_action(owner, action)
        else:
            rollout.decide(owner, key=action, sample=False)

    def advance(max_points, max_seconds):
        before = rollout.totals[rollout.learner_owner]
        started = time.monotonic()
        print(json.dumps({"event": "advance_start", "tick": rollout.tick,
                          "limit_seconds": max_seconds}), flush=True)
        while not rollout.terminal and not rollout.ready:
            rollout.step_native()
            if time.monotonic() - started >= max_seconds:
                raise TimeoutError("native transition exceeded PUCT horizon")
        elapsed = time.monotonic() - started
        if elapsed > max_seconds + 1e-9:
            raise TimeoutError("native transition exceeded PUCT horizon")
        print(json.dumps({"event": "advance_end", "tick": rollout.tick,
                          "elapsed_seconds": elapsed}), flush=True)
        return rollout.totals[rollout.learner_owner] - before, 1, elapsed

    adapter = NativeSnapshotSearchAdapter(
        native, capture=capture,
        restore_sidecar=lambda state: rollout.restore_search(state.policy_state["rollout"]),
        apply=apply, advance=advance, battle_sidecar=BattleEnvSidecar(env),
        copy_capture=False, profile_enabled=False,
        capture_root_capsule=(rollout.capture_root_capsule if ROOT_CAPSULE else None),
        restore_root_capsule=(rollout.restore_root_capsule if ROOT_CAPSULE else None),
    )

    def prior(state):
        if BF16 and active_mode != "baseline":
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                return (rollout.packed_policy_prior(
                    state.actor)
                        if PACKED_PRIOR else rollout.policy_prior(state.actor))
        return (rollout.packed_policy_prior(state.actor) if PACKED_PRIOR
                else rollout.policy_prior(state.actor))

    def evaluate(state):
        owner = rollout.learner_owner
        if active_mode != "baseline" and rollout.ready and rollout.ready[0] == owner:
            with torch.inference_mode():
                value = float(rollout.context(owner, profile_span="leaf.context_encode_ms").value.reshape(-1)[0])
            observation = env.observe(owner=owner, policy_features_only=True)
            potential = (tower_potential(
                tower_state(observation.towers, rollout.initial_tower_hp), owner
            ) if shaping is not None else 0.0)
            return unshape_bootstrap(
                value, potential, shaping["coefficient"] if shaping is not None else 0.0
            )
        tensorizer = rollout.tensorizers[owner]
        saved = _tensorizer_snapshot(tensorizer)
        try:
            observation = env.observe(owner=owner, policy_features_only=True)
            frame = canonicalize_event_observation(
                tensorizer.tensorize(observation, validate=False)
            )
            with torch.inference_mode(), torch.autocast(
                    device_type="cuda", dtype=torch.bfloat16, enabled=BF16):
                rollout._count_profile("model_context_encode_calls")
                started = time.perf_counter()
                try:
                    context = policy.model.encode(
                        frame.to_model_input("cuda:0"),
                        policy.model.initial_state(1, device="cuda:0"),
                        episode_start=torch.ones(1, dtype=torch.bool, device="cuda:0"),
                    )
                finally:
                    rollout._profile_span("leaf.context_encode_ms", started)
                value = float(context.value.reshape(-1)[0])
            potential = (tower_potential(
                tower_state(observation.towers, rollout.initial_tower_hp), owner
            ) if shaping is not None else 0.0)
            return unshape_bootstrap(
                value, potential, shaping["coefficient"] if shaping is not None else 0.0
            )
        finally:
            _restore_tensorizer(tensorizer, saved)

    def capture_leaf(state):
        """Capture the learner frame while this leaf is the live native world."""
        owner = rollout.learner_owner
        observations = rollout.current_policy_observations()
        observation = observations[owner]
        tensorizer = rollout.tensorizers[owner]
        saved = _tensorizer_snapshot(tensorizer)
        try:
            started = time.perf_counter()
            frame = canonicalize_event_observation(
                tensorizer.tensorize(observation, validate=False)
            )
            rollout._profile_span("leaf.batch_tensorize_ms", started)
        finally:
            _restore_tensorizer(tensorizer, saved)
        potential = (tower_potential(
            tower_state(observation.towers, rollout.initial_tower_hp), owner
        ) if shaping is not None else 0.0)
        return {"frame": frame, "potential": potential}

    def evaluate_batch(items):
        if not items:
            return []
        if any(not isinstance(item, dict) or "frame" not in item for item in items):
            raise ValueError("batch leaf item is missing a captured frame")
        started = time.perf_counter()
        with torch.inference_mode(), torch.autocast(
                device_type="cuda", dtype=torch.bfloat16, enabled=BF16):
            rollout._count_profile("model_context_encode_calls")
            packed = _stack_frames(tuple(item["frame"] for item in items)).to_model_input(
                "cuda:0", float_dtype=torch.float32
            )
            context = policy.model.encode(
                packed,
                policy.model.initial_state(len(items), device="cuda:0"),
                episode_start=torch.ones(len(items), dtype=torch.bool, device="cuda:0"),
            )
            values = context.value.reshape(-1).detach().float().cpu().tolist()
        rollout._profile_span("leaf.batch_context_encode_ms", started)
        coefficient = shaping["coefficient"] if shaping is not None else 0.0
        return [unshape_bootstrap(value, item["potential"], coefficient)
                for value, item in zip(values, items)]

    records = []
    checkpoint = None
    root = None
    try:
        rollout.reset(decks=decks, seed=SEED)
        while not rollout.ready and not rollout.terminal:
            rollout.step_native()
        while not rollout.terminal and rollout.ready[0] != rollout.learner_owner:
            rollout.decide(rollout.ready[0], sample=True)
        if rollout.terminal:
            raise RuntimeError("match ended before the profile root")
        if os.environ.get("PUCT_CAPTURE_ONLY") == "1":
            rollout.begin_search_root(search_only=True)
            checkpoint = adapter.checkpoint_restorable()
            view = adapter.capture_current_view()
            root_hash = state_digest(checkpoint.state)
            root_fingerprint = adapter.fingerprint()
            components = component_hashes(checkpoint.state)
            paths = field_hashes(checkpoint.state)
            native_snapshot_metadata = {
                key: value for key, value in checkpoint.state.native_snapshot.items()
                if key not in {"handle", "snapshotHandles", "stateEpoch"}
            }
            audit_values = {
                "event_cursor": audit_normalize(checkpoint.state.event_cursor),
                "native_snapshot_operational": audit_normalize({
                    key: value for key, value in checkpoint.state.native_snapshot.items()
                    if key not in {"handle", "stateEpoch"}
                }),
                "fair_episode_ids": {
                    owner: observation.get("episode_id")
                    for owner, observation in checkpoint.state.observation.get("fair", {}).items()
                },
                "native_observation_generation": checkpoint.state.observation.get(
                    "native", {}).get("generation"),
                "rollout_tensorizers": audit_normalize(
                    checkpoint.state.policy_state.get("rollout", {}).get("tensorizers")
                ),
            }
            adapter.release(checkpoint)
            checkpoint = None
            status = (native._raw_request("status")
                      if isinstance(native, ResidentNativeClashEnv)
                      else native.status())
            capture = {
                "schema": "firstlight-search-state-component-audit.v1",
                "episode_id": EPISODE_ID,
                "port": PORT,
                "seed": SEED,
                "tick": view.observation["tick"],
                "root_state_sha256": root_hash,
                "root_fingerprint": root_fingerprint,
                "native_snapshot_metadata": native_snapshot_metadata,
                "field_values": audit_values,
                "component_hashes": components,
                "field_hashes": paths,
                "snapshot_handles_after": snapshot_count(status),
            }
            OUTPUT.write_text(json.dumps(capture, indent=2, sort_keys=True) + "\n")
            print(json.dumps(capture, sort_keys=True), flush=True)
            return

        for index in range(PROFILE_TRIALS * len(PROFILE_MODES)):
            trial, mode_index = divmod(index, len(PROFILE_MODES))
            mode = PROFILE_MODES[mode_index]
            if mode not in {"baseline", "candidate", "candidate-old", "candidate-fast",
                            "candidate-cache",
                            "candidate-view", "candidate-cache-view"}:
                raise ValueError(f"unsupported P11 A/B mode: {mode}")
            active_mode = mode
            rollout.context_cache_enabled = mode in {"candidate-cache",
                                                     "candidate-cache-view"}
            setup_started = time.perf_counter()
            rollout.begin_search_root(search_only=mode != "baseline")
            checkpoint = adapter.checkpoint_restorable()
            view = (adapter.view_from_checkpoint(checkpoint)
                    if mode in {"candidate-view", "candidate-cache-view"}
                    else adapter.capture_current_view())
            setup_ms = (time.perf_counter() - setup_started) * 1000.0
            root_hash = state_digest(checkpoint.state)
            root_fingerprint = adapter.fingerprint()
            components = component_hashes(checkpoint.state)
            adapter.profile_enabled = True
            rollout.profile_counters = {}
            rollout.profile_sink = adapter._add_profile
            recorder_rows_before = sum(map(len, rollout.recorder.rows.values()))
            adapter.begin_profile_root(f"{EPISODE_ID}:{mode}:{trial}", SIMULATIONS)
            search_options = {
                "cpuct": tree["cpuct"],
                "gamma": config["optimizer"]["gamma"],
                "max_decision_points": tree["max_decision_points"],
                "max_native_seconds": tree["max_native_seconds"],
                "root_noise": False,
                "dirichlet_alpha": tree.get("dirichlet_alpha", 0.25),
                "dirichlet_epsilon": tree.get("dirichlet_epsilon", 0.25),
                "opponent_top_k": 4,
                "opponent_temperature": 0.5,
                "seed": SEED,
                "compact_children": True,
                "edge_memo": True,
                "edge_verify_every": 1,
                "edge_component_diagnostics": False,
                "transition_discount": LearnerDecisionDiscount(
                    0, config["optimizer"]["gamma"]
                ),
                "evaluate_batch": (evaluate_batch if LEAF_BATCH_SIZE > 1 else None),
                "capture_leaf": (capture_leaf if LEAF_BATCH_SIZE > 1 else None),
                "leaf_batch_size": LEAF_BATCH_SIZE,
            }
            supported_options = inspect.signature(PuctSearch).parameters
            search = PuctSearch(
                adapter, prior, prior, evaluate,
                **{key: value for key, value in search_options.items()
                   if key in supported_options},
            )
            started = time.perf_counter()
            root = search.run(
                SIMULATIONS, root_state=view, root_checkpoint=checkpoint
            )
            wall_ms = (time.perf_counter() - started) * 1000.0
            if not root.visits:
                raise RuntimeError("PUCT completed without recording a root visit")
            selected_token = max(
                root.visits,
                key=lambda action: (
                    root.visits[action],
                    (-root.action_space.canonical_tie_rank[action]
                     if root.action_space is not None else action),
                ),
            )
            decode = (root.action_space.decode_one if root.action_space is not None
                      else lambda action: action)
            selected = decode(selected_token)
            visits = {decode(action): int(count) for action, count in root.visits.items() if count}
            q_values = {decode(action): float(root.q(action))
                        for action, count in root.visits.items() if count}
            total_visits = sum(visits.values())
            teacher_policy = {
                action: count / total_visits for action, count in visits.items()
            }
            fingerprint_match = adapter.fingerprint() == root_fingerprint
            summary = adapter.profile_summary()
            summary["counters"].update(rollout.profile_counters)
            if mode != "baseline":
                if summary["counters"].get("model_evaluate_action_calls", 0) != 0:
                    raise RuntimeError("P11 candidate evaluated predetermined search actions")
            rollout.audit_search_root()
            recorder_rows_after = sum(map(len, rollout.recorder.rows.values()))
            if recorder_rows_after != recorder_rows_before:
                raise RuntimeError("search branch changed persistent training row count")
            records.append({
                "mode": mode, "trial": trial, "root_tick": view.observation["tick"],
                "root_hash": root_hash, "selected_action": selected,
                "visit_counts": visits, "q_values": q_values,
                "teacher_policy": teacher_policy, "wall_ms": wall_ms,
                "setup_ms": setup_ms,
                "leaf_batch_size": LEAF_BATCH_SIZE,
                "recorder_rows_before": recorder_rows_before,
                "recorder_rows_after": recorder_rows_after,
                "fingerprint_match": fingerprint_match, "profile_summary": summary,
            })
            PARTIAL_OUTPUT.write_text(json.dumps({
                "schema": "firstlight-puct-p11-search-only-ab.partial.v1",
                "records": records,
            }, separators=(",", ":"), allow_nan=False) + "\n")
            search.release(root)
            root = None
            if not fingerprint_match:
                raise RuntimeError("root fingerprint mismatch after PUCT")
            print(json.dumps({
                "event": "before_checkpoint_release",
                "mode": mode,
                "trial": trial,
                "adapter_owned": sorted(adapter._owned),
                "adapter_owned_count": adapter.owned_snapshot_count,
                "slot_status": native.status(),
                "resident_status": native.resident_status(),
            }, sort_keys=True), flush=True)
            adapter.release(checkpoint)
            checkpoint = None
            rollout.end_search_root()
            status = native._raw_request("status")
            print(json.dumps({
                "event": "after_checkpoint_release",
                "mode": mode,
                "trial": trial,
                "adapter_owned": sorted(adapter._owned),
                "adapter_owned_count": adapter.owned_snapshot_count,
                "slot_status": status,
                "resident_status": native.resident_status(),
            }, sort_keys=True), flush=True)
            if adapter.owned_snapshot_count != 0 or snapshot_count(status) != 0:
                raise RuntimeError("snapshot handles did not return to zero after sample")
            print(json.dumps({key: records[-1][key] for key in (
                "mode", "trial", "root_tick", "selected_action", "wall_ms",
                "fingerprint_match",
            )}), flush=True)
    finally:
        if root is not None:
            try:
                search.release(root)
            except Exception:
                pass
        if checkpoint is not None:
            adapter.release(checkpoint)
        native.close_transport()
        lock.close()

    status = service.request(PORT, "status")
    if snapshot_count(status) != 0:
        raise RuntimeError("snapshot handles did not return to zero")
    by_mode = {
        mode: [item["wall_ms"] for item in records if item["mode"] == mode]
        for mode in PROFILE_MODES
    }
    semantics = {
        (item["selected_action"],
         json.dumps(item["visit_counts"], sort_keys=True),
         json.dumps(item["q_values"], sort_keys=True),
         json.dumps(item["teacher_policy"], sort_keys=True))
        for item in records
    }
    medians = {
        mode: statistics.median(values) for mode, values in by_mode.items() if values
    }
    baseline_median = medians.get("candidate-old", medians.get("baseline"))
    candidate_median = medians.get("candidate-fast", medians.get("candidate"))
    result = {
        "schema": "firstlight-puct-p11-search-only-ab.v1",
        "checkpoint_sha256": policy.checkpoint_sha256,
        "native_probe_sha256": PROBE_SHA256,
        "port": PORT, "seed": SEED, "simulations": SIMULATIONS,
        "trials_per_mode": PROFILE_TRIALS, "records": records,
        "median_wall_ms_by_mode": medians,
        "median_speedup_percent": (
            (baseline_median - candidate_median) / baseline_median * 100.0
            if baseline_median is not None and candidate_median is not None else None
        ),
        "min_wall_ms_by_mode": {
            mode: min(values) for mode, values in by_mode.items() if values
        },
        "max_wall_ms_by_mode": {
            mode: max(values) for mode, values in by_mode.items() if values
        },
        "p11_candidate_model_evaluate_action_calls": [
            item["profile_summary"]["counters"].get("model_evaluate_action_calls", 0)
            for item in records if item["mode"] != "baseline"
        ],
        "training_row_counts_stable": all(
            item["recorder_rows_before"] == item["recorder_rows_after"] for item in records
        ),
        "root_hash_stable_by_mode": {
            mode: len({item["root_hash"] for item in records if item["mode"] == mode}) == 1
            for mode in PROFILE_MODES
        },
        "search_semantics_stable": len(semantics) == 1,
        "all_fingerprints_match": all(item["fingerprint_match"] for item in records),
        "snapshot_handles_after": snapshot_count(status),
        "component_hashes": components,
        "source_identity": {
            "source_inventory_sha256": file_sha256(PROJECT / "FILE_SHA256SUMS.txt"),
            "native_probe_sha256": PROBE_SHA256,
            "config_sha256": file_sha256(ROOT / "config.json"),
            "plan_sha256": file_sha256(ROOT / "match-plan.json"),
            "checkpoint_sha256": policy.checkpoint_sha256,
            "baseline_tree_search_sha256": file_sha256(ROOT / "tree_search.py"),
            "baseline_native_adapter_sha256": file_sha256(ROOT / "native_adapter.py"),
            "candidate_tree_search_sha256": file_sha256(P10_ROOT / "tree_search.py"),
            "candidate_native_adapter_sha256": file_sha256(P10_ROOT / "native_adapter.py"),
            "candidate_event_policy_rollout_sha256": file_sha256(P10_ROOT / "event_policy_rollout.py"),
            "probe_script_sha256": file_sha256(__file__),
        },
    }
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if PARTIAL_OUTPUT.exists():
        PARTIAL_OUTPUT.unlink()
    print(json.dumps({key: value for key, value in result.items()
                      if key not in {"records", "component_hashes"}},
                     indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
