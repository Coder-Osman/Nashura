"""Single-actor SSPI self-play collection on the FAIR native rollout runtime."""

from dataclasses import asdict
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time

import torch

from native_runner.cr_native_env import NativeClashEnv, ResidentNativeClashEnv, RunnerError, JsonEventCursor
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.resident_batch_channel import BatchStepFailureV1, ResidentBatchChannelV1
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
from ..fair_final.codec import ActionCodec
from ..fair_final.episode import terminal_result
from ..fair_final.history import EventBatchBoundary, FairHistoryRecorder
from ..fair_final.history_belief import HistoryBeliefFactory
from ..fair_final.native import FinalBattleEnv
from ..fair_final.policy import FullAction
from ..fair_final.search import CompletedBoundarySearch, SearchLimits, split_seed
from ..fair_final.search_runtime import RolloutSearchWorld
from ..fair_final.history_provider import RecoveringHistoryCursor
from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import pack, semantic_digest
from ..fair_final.visibility import validate_fair_view
from .learning import SEARCH_BUDGETS
from .augmentation import TemporalAugmenter, TEMPORAL_SCHEMA
from .protocol import ACTION_SCHEMA, ActionMode, SSPIAction, from_full_action, to_full_action
from .rollout import make_sspi_rollout
from .search import budget_search_sha, make_search_inference, SSPIHistorySearchProvider

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "research" / "puct-runtime"))
from native_adapter import BattleEnvSidecar


ARM_CONFIG = {
    "A_5PCT_128": (0.05, 128),
}
EPISODE_SCHEMA = "sspi-mac-pilot-episode.v1"


@contextmanager
def _timed(profile, name):
    if profile is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        profile[name] = profile.get(name, 0.) + time.perf_counter() - started


class SearchNative(NativeClashEnv):
    """Use the selected, already-verified control transport for an AVD worker."""

    def __init__(self, host, port, *, transport="legacy", timeout=90.):
        if transport not in ("legacy", "persistent", "persistent-delta"):
            raise ValueError("SSPI native transport must be legacy or persistent")
        super().__init__(host, port, timeout=timeout)
        self.transport = transport
        self._persistent_transport_requested = transport in ("persistent", "persistent-delta")
        self.json_event_deltas = transport == "persistent-delta"

    def start_atomic_steps(self, *, initial_tick=None):
        return None


class EventBatchedSSPINative(ResidentNativeClashEnv):
    """Return complete validated JSON captures at native event/deadline boundaries."""

    supports_event_batched_advance = True

    _CHANNEL_PROFILE_FIELDS = (
        "connect_ns", "batches", "environments", "request_bytes", "response_bytes",
        "decoded_response_bytes", "send_ns", "request_ack_ns", "last_request_ack_ns",
        "receive_ns", "decode_ns", "total_ns",
    )

    def __init__(self, host, port, *, env_id=0, timeout=90., event_deltas=False,
            persistent_control=False):
        super().__init__(env_id, host=host, port=port, timeout=timeout)
        if type(persistent_control) is not bool:
            raise ValueError("SSPI event-batch control transport switch must be boolean")
        self._persistent_transport_requested = persistent_control
        self._batch_channel = None
        self._json_cursors = {env_id: JsonEventCursor()}
        self.event_deltas = bool(event_deltas)
        self._capture = None
        self._atomic_steps = False
        self._last_tick = None
        self.last_advance_boundary = None
        self.capture_profile = {name: 0 for name in self._CHANNEL_PROFILE_FIELDS}
        self.capture_profile.update(actual_native_ticks=0, requested_native_ticks=0,
            host_observations_avoided=0, stopped_on_deployment=0, stopped_on_terminal=0,
            reached_tick_budget=0)

    def _close_batch(self):
        channel, self._batch_channel = self._batch_channel, None
        if channel is None:
            return
        profile = channel.profile()
        for name in self._CHANNEL_PROFILE_FIELDS:
            self.capture_profile[name] += int(profile[name])
        channel.close()

    def _request(self, command, *, _read_retry_count=2):
        with self._lock:
            self._close_batch()
            self._capture = None
            if command == "snapshot-create" or command.startswith(
                    ("restore ", "release-snapshot ")):
                status = self.resident_status()
                slots = status.get("slots", ())
                if (status.get("occupied") != 1 or len(slots) != 1
                        or slots[0].get("envId") != self.env_id):
                    raise RunnerError("SSPI batched control requires exactly one resident slot")
                if status.get("residentSnapshotRestore") is not True:
                    raise RunnerError("SSPI event-batched MCTS requires resident snapshot/restore support")
                result = super()._request(command, _read_retry_count=_read_retry_count)
            else:
                result = super()._request(command, _read_retry_count=_read_retry_count)
            if command == "reset" or command.startswith(("configure ", "configure-native ")):
                self._json_cursors[self.env_id] = JsonEventCursor()
                self._last_tick = None
                self.last_advance_boundary = None
            elif command.startswith("restore "):
                self._json_cursors[self.env_id] = JsonEventCursor()
            return result

    def restore(self, handle):
        observation = super().restore(handle)
        tick = observation.get("tick")
        if type(tick) is not int or tick < 0:
            raise RunnerError("SSPI event-batch restore returned an invalid native tick")
        with self._lock:
            self._last_tick = tick
            self.last_advance_boundary = None
        return observation

    def start_atomic_steps(self, *, initial_tick=None):
        with self._lock:
            self._capture = None
            self.last_advance_boundary = None
            if type(initial_tick) is not int or initial_tick < 0:
                raise ValueError("initial native tick must be a nonnegative integer")
            self._last_tick = initial_tick
            self._atomic_steps = True

    def step(self, ticks=1):
        if not self._atomic_steps:
            self.last_advance_boundary = None
            return super().step(ticks)
        if type(ticks) is not int or not 1 <= ticks <= 1_000_000:
            raise ValueError("SSPI batched transport requires a positive native tick span")
        with self._lock:
            self._capture = None
            if self._batch_channel is None:
                super().close_transport()
                self._batch_channel = ResidentBatchChannelV1(
                    self.host, self.port, timeout=self.timeout, training_capture=False,
                    compressed_json=False, validate_json_capture=True, step_until_event=True,
                    **({"json_event_deltas": True, "json_cursors": self._json_cursors}
                       if self.event_deltas else {}))
            try:
                outcome = self._batch_channel.step_partitioned({self.env_id: ()}, ticks=ticks)[self.env_id]
                if isinstance(outcome, BatchStepFailureV1):
                    raise outcome.error
                capture = outcome.capture
                ordinary = capture.get("ordinary") if isinstance(capture, Mapping) else None
                if not isinstance(ordinary, Mapping):
                    raise RunnerError("SSPI event batch returned no ordinary state")
                current_tick = ordinary.get("tick")
                if type(current_tick) is not int:
                    raise RunnerError("SSPI event batch returned an invalid native tick")
                if self._last_tick is not None:
                    start_tick = self._last_tick
                    advanced = current_tick - self._last_tick
                    if not 1 <= advanced <= ticks:
                        raise RunnerError("SSPI event batch tick delta is outside its request")
                    self.capture_profile["actual_native_ticks"] += advanced
                    self.capture_profile["host_observations_avoided"] += advanced - 1
                self.capture_profile["requested_native_ticks"] += ticks
                self.capture_profile["stopped_on_deployment"] += int(outcome.stopped_on_deployment)
                self.capture_profile["stopped_on_terminal"] += int(outcome.stopped_on_terminal)
                self.capture_profile["reached_tick_budget"] += int(outcome.reached_max_ticks)
                stop_flags = (int(outcome.stopped_on_deployment) + int(outcome.stopped_on_terminal)
                    + int(outcome.reached_max_ticks))
                if stop_flags != 1 or self._last_tick is None:
                    raise RunnerError("SSPI event batch returned ambiguous boundary evidence")
                stop_reason = ("deployment" if outcome.stopped_on_deployment else
                    "terminal" if outcome.stopped_on_terminal else "tick_budget")
                self.last_advance_boundary = EventBatchBoundary(
                    start_tick, current_tick, stop_reason)
                self._last_tick = current_tick
                self._capture = capture
                return ordinary
            except BaseException:
                self._close_batch()
                raise

    def observe(self):
        if self._capture is not None:
            return self._capture["ordinary"]
        return super().observe()

    def observe_atomic(self):
        if self._capture is not None:
            return self._capture
        if self.event_deltas:
            cursor = self._json_cursors[self.env_id]
            result = self._request("observe-atomic-delta " + " ".join(map(str, cursor.wire())))
            self._capture = cursor.validate(result)
            return self._capture
        return super().observe_atomic()

    def finish_capture_profile(self):
        with self._lock:
            self._close_batch()
            return dict(self.capture_profile)

    def close_transport(self):
        with self._lock:
            self._close_batch()
            self._capture = None
            super().close_transport()

    def close(self):
        try:
            self.close_transport()
        finally:
            super().close()


def resolve_belief_ports(actual_port, belief_port, belief_ports=None):
    if type(actual_port) is not int or not 1 <= actual_port <= 65535:
        raise ValueError("SSPI actual native port must be a valid TCP port")
    if belief_ports is None:
        if type(belief_port) is not int or not 1 <= belief_port < 65535:
            raise ValueError("SSPI belief port must leave room for both owner workers")
        belief_ports = {0: belief_port, 1: belief_port + 1}
    elif isinstance(belief_ports, Mapping):
        belief_ports = dict(belief_ports)
    else:
        raise ValueError("SSPI belief ports must map both owners to native ports")
    if (set(belief_ports) != {0, 1}
            or any(type(owner) is not int for owner in belief_ports)
            or any(type(port) is not int or not 1 <= port <= 65535
                for port in belief_ports.values())
            or len(set(belief_ports.values())) != 2
            or actual_port in belief_ports.values()):
        raise ValueError("SSPI requires distinct actual and per-owner belief ports")
    return {owner: belief_ports[owner] for owner in (0, 1)}


def make_belief_native(host, port, *, transport, timeout=90.):
    if transport in ("event-json", "event-json-delta"):
        return EventBatchedSSPINative(host, port, timeout=timeout,
            event_deltas=transport == "event-json-delta", persistent_control=True)
    return SearchNative(host, port, transport=transport, timeout=timeout)


def preflight_native_endpoints(host, actual_port, belief_ports, *, transport, actual_env_id=0,
        ruleset_manifest=None, include_belief=True):
    if type(actual_env_id) is not int or not 0 <= actual_env_id < 16:
        raise ValueError("SSPI actual resident env id must be in 0..15")
    if type(include_belief) is not bool:
        raise ValueError("SSPI belief preflight switch must be a boolean")
    if actual_env_id != 0 and transport not in ("event-json", "event-json-delta"):
        raise ValueError("non-default actual env ids require event-json resident transport")
    endpoints = [("actual", actual_port)]
    if include_belief:
        endpoints.extend((("belief_owner_0", belief_ports[0]),
            ("belief_owner_1", belief_ports[1])))
    attestations = {}
    expected_identity = None
    for role, port in endpoints:
        cold_belief_worker = False
        if transport in ("event-json", "event-json-delta") and role == "actual":
            probe = SearchNative(host, port, transport="persistent", timeout=90.)
            try:
                resident = probe._request("multi-status")
            finally:
                probe.close_transport()
            slots = resident.get("slots", ())
            if (resident.get("mode") != "resident-headless"
                    or resident.get("occupied") != len(slots)
                    or not any(slot.get("envId") == actual_env_id for slot in slots)):
                raise RunnerError("event-json requires the selected existing resident actual match")
            native = ResidentNativeClashEnv(actual_env_id, host, port, timeout=90.)
        elif transport in ("event-json", "event-json-delta"):
            event_native = make_belief_native(host, port, transport=transport, timeout=90.)
            resident = event_native.resident_status()
            slots = resident.get("slots", ())
            if (resident.get("mode") == "resident-headless" and resident.get("occupied") == 1
                    and len(slots) == 1 and slots[0].get("envId") == event_native.env_id
                    and slots[0].get("ended") is True):
                native = event_native
            elif (resident.get("mode") == "legacy-headless" and resident.get("active") is False
                    and resident.get("occupied") == 0 and not slots):
                event_native.close_transport()
                native = SearchNative(host, port, transport="persistent", timeout=90.)
                cold_belief_worker = True
            else:
                event_native.close_transport()
                raise RunnerError(f"SSPI event-batched belief endpoint must have one resident slot on port {port}")
        else:
            native = SearchNative(host, port, transport=transport, timeout=90.)
        try:
            status = native.status()
            if cold_belief_worker:
                if (status.get("ready") is not False or status.get("coldReady") is not True
                        or status.get("configured") is not False or status.get("tick") != -1):
                    raise ValueError(f"SSPI event-batched belief endpoint is not cold on port {port}")
            elif status.get("ready") is not True:
                raise ValueError(f"SSPI native {role} endpoint is not ready on port {port}")
            if not cold_belief_worker:
                observation = native.observe()
                if observation.get("ok") is not True:
                    raise ValueError(f"SSPI native {role} endpoint returned an invalid observation")
            attestation = native.attest(ruleset_manifest)
            identity = dict(libg_sha256=attestation.libg_sha256,
                probe_sha256=attestation.probe_sha256)
            if any(not isinstance(value, str) or len(value) != 64 for value in identity.values()):
                raise ValueError(f"SSPI native {role} endpoint has an incomplete attestation")
            if expected_identity is not None and identity != expected_identity:
                raise ValueError("SSPI native endpoints do not share the same engine and probe")
            expected_identity = identity
            attestations[role] = identity
        finally:
            native.close_transport()
    return attestations


class OwnerBeliefProviders:
    """Keep each player's public-history cursor warm across alternating searches."""

    def __init__(self, ports, factory):
        if set(ports) != {0, 1} or not callable(factory):
            raise ValueError("belief provider cache requires both owner ports and a factory")
        self.ports = dict(ports)
        self.factory = factory
        self.providers = {}

    def for_search(self, owner, history):
        if type(owner) is not int or owner not in (0, 1):
            raise ValueError("belief provider owner must be zero or one")
        provider = self.providers.get(owner)
        if provider is None:
            provider = self.factory(owner, self.ports[owner], history)
            self.providers[owner] = provider
        else:
            provider.update_history(history)
        return provider

    def close(self):
        providers, self.providers = self.providers, {}
        failures = []
        for provider in providers.values():
            try:
                provider.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise RuntimeError("one or more owner belief providers failed to close") from failures[0]


def decision_gate(seed, *, run_id, arm, game_id, owner, decision_index, probability):
    if (type(probability) is not float or not 0. <= probability < 1.
            or type(owner) is not int or owner not in (0, 1)
            or type(decision_index) is not int or decision_index < 0):
        raise ValueError("invalid SSPI per-decision search gate")
    gate_seed = split_seed(seed, "sspi-search-gate.v1", run_id, arm, game_id, owner, decision_index)
    if probability == 0.:
        return False, gate_seed
    return random.Random(gate_seed).random() < probability, gate_seed


def episode_seed_identity(seed, *, run_id, arm, game_id, random_stream_id=None):
    stream_id = game_id if random_stream_id is None else random_stream_id
    if (type(stream_id) is not str or not stream_id or len(stream_id) > 160
            or stream_id.strip() != stream_id):
        raise ValueError("random stream identity must be a non-empty canonical string")
    return dict(random_stream_id=stream_id,
        actual_match_seed=split_seed(seed, "actual-match", run_id, arm, stream_id) % (2**31 - 1),
        actual_rollout_seed=split_seed(seed, "actual-rollout", run_id, arm, stream_id))


def _atomic_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def _atomic_torch(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    torch.save(value, buffer)
    data = buffer.getvalue()
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".",
            suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return hashlib.sha256(data).hexdigest()


def _action_key(action):
    return from_full_action(action).key


def _native_execution_action(action, *, mode):
    if mode in ("DIRECT", "SEARCH") and isinstance(action, FullAction):
        return action
    if mode == "DIRECT" and isinstance(action, SSPIAction):
        return to_full_action(action)
    raise TypeError("SSPI decision mode returned an incompatible action type")


def _source_sha():
    files = (
        Path(__file__),
        Path(__file__).with_name("protocol.py"),
        Path(__file__).with_name("model.py"),
        Path(__file__).with_name("learning.py"),
        Path(__file__).with_name("augmentation.py"),
        Path(__file__).with_name("rollout.py"),
        Path(__file__).with_name("search.py"),
        PROJECT / "native_runner" / "cr_native_env.py",
        PROJECT / "native_runner" / "battle_env.py",
        PROJECT / "native_runner" / "training" / "tracking.py",
        PROJECT / "native_runner" / "resident_batch_channel.py",
        PROJECT / "native_runner" / "runtime_schema.py",
        PROJECT / "native_runner" / "rich_telemetry_adapter.py",
        PROJECT / "native_runner" / "special_movement_runtime.py",
        PROJECT / "native_runner" / "action_movement_runtime.py",
        PROJECT / "native_runner" / "character_state_runtime.py",
        PROJECT / "native_runner" / "training_capture.py",
        PROJECT / "native_runner" / "match_factory.py",
        PROJECT / "native_runner" / "training" / "event_action_space.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "history.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "history_belief.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "history_provider.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "native.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "policy.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "rollout.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "search.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "search_runtime.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "timing.py",
        PROJECT / "native_runner" / "training" / "fair_final" / "visibility.py",
        PROJECT / "native_runner" / "training" / "fair_search" / "belief.py",
        PROJECT / "native_runner" / "training" / "fair_search" / "contracts.py",
        PROJECT / "native_runner" / "training" / "az_joint" / "model.py",
        PROJECT / "native_runner" / "training" / "v4" / "model.py",
        PROJECT / "native_runner" / "training" / "v4" / "components.py",
        PROJECT / "native_runner" / "training" / "v4" / "factory.py",
        PROJECT / "research" / "puct-runtime" / "native_adapter.py",
    )
    return semantic_digest({path.relative_to(PROJECT).as_posix():
        hashlib.sha256(path.read_bytes()).hexdigest() for path in files})


def _make_environment(native, bundle):
    return FinalBattleEnv(native=native, card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog))


def _root_visits(outcome, codec):
    visits = []
    for candidate in outcome.result["candidates"]:
        count = candidate["visits"]
        if count:
            action = from_full_action(codec.decode(candidate["action_id"]))
            visits.append((action.key, int(count)))
    if sum(count for _, count in visits) != outcome.result["valid_simulations"]:
        raise ValueError("SSPI root does not contain its complete effective visit distribution")
    return visits


def _observe_histories(rollout, histories, *, timing_profile=None):
    environment = rollout.runtime.environment
    with _timed(timing_profile, "history_fair_view_seconds"):
        views = dict(rollout.runtime.frozen)
        if not views:
            views = environment.observe_dual(policy_features_only=True)
        else:
            for owner in (0, 1):
                if owner not in views:
                    views[owner] = environment.observe(owner=owner, policy_features_only=True)
    boundary = getattr(environment.native, "last_advance_boundary", None)
    for owner in (0, 1):
        with _timed(timing_profile, "history_fair_validation_seconds"):
            validate_fair_view(views[owner], owner)
        with _timed(timing_profile, "history_recorder_seconds"):
            histories[owner].observe_tick(views[owner], interval=boundary)
    return views


def collect_episode(*, actual_port, belief_port, output_dir, run_id, arm, game_id,
        seed=20261101, model=None, models_by_owner=None, device="cpu", host="127.0.0.1",
        transport="legacy", search_probability=None, episode_kind="training", progress=None,
        temporal_augmentation=True, random_stream_id=None, belief_ports=None, actual_env_id=0):
    """Collect one complete native match; incomplete matches are quarantined."""
    try:
        belief_ports = resolve_belief_ports(actual_port, belief_port, belief_ports)
    except ValueError:
        raise
    if (arm not in ARM_CONFIG or type(seed) is not int or seed < 0
            or type(actual_env_id) is not int or not 0 <= actual_env_id < 16
            or (actual_env_id != 0 and transport not in ("event-json", "event-json-delta"))
            or transport not in ("legacy", "persistent", "event-json", "event-json-delta")):
        raise ValueError("SSPI collection requires a fixed arm, distinct native ports, and explicit seed")
    if device not in ("cpu", "mps") or (device == "mps" and not torch.backends.mps.is_available()):
        raise ValueError("requested SSPI inference device is unavailable")
    probability, simulations = ARM_CONFIG[arm]
    if SEARCH_BUDGETS[arm] != simulations:
        raise AssertionError("SSPI arm and learning budget registry differ")
    if episode_kind not in ("training", "validation", "evaluation"):
        raise ValueError("SSPI episode kind must be training, validation, or evaluation")
    if type(temporal_augmentation) is not bool:
        raise ValueError("temporal augmentation switch must be a boolean")
    augmenter = TemporalAugmenter() if temporal_augmentation and episode_kind != "evaluation" else None
    if search_probability is None:
        search_probability = probability
    if type(search_probability) is not float or not 0. <= search_probability < 1.:
        raise ValueError("SSPI search probability must be in [0, 1)")
    if ((episode_kind == "evaluation") != (search_probability == 0.)
            or (episode_kind == "training" and search_probability == 0.)):
        raise ValueError("only evaluation episodes may disable MCTS")
    seed_identity = episode_seed_identity(seed, run_id=run_id, arm=arm, game_id=game_id,
        random_stream_id=random_stream_id)
    training_admissible = episode_kind == "training"
    if model is not None and models_by_owner is not None:
        raise ValueError("provide either one shared model or owner-specific models")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(1)

    from ..v4.factory import production_semantic_bundle
    from .model import initialize_fresh_model

    bundle = production_semantic_bundle()
    if models_by_owner is None:
        shared_model = model or initialize_fresh_model(seed, device=device, bundle=bundle)
        models_by_owner = {0: shared_model, 1: shared_model}
    if (not isinstance(models_by_owner, dict) or set(models_by_owner) != {0, 1}
            or any(not isinstance(item, torch.nn.Module) for item in models_by_owner.values())):
        raise ValueError("SSPI collection requires one model for each owner")
    for item in models_by_owner.values():
        item.to(device).eval()
        for parameter in item.parameters():
            parameter.requires_grad_(False)
    model_sha_by_owner = {str(owner): semantic_digest(item.state_dict())
        for owner, item in sorted(models_by_owner.items())}
    belief_transport = transport
    controller_sha_by_owner = {owner: semantic_digest(dict(model_sha=model_sha_by_owner[str(owner)],
        action_schema=ACTION_SCHEMA, delay_ms=[200, 400, 600, 800, 1000],
        wait_ticks=20, device=device, deck=list(HOG_DECK), native_transport=transport,
        belief_transport=belief_transport))
        for owner in (0, 1)}
    inference_by_owner = {owner: make_search_inference(models_by_owner[owner],
        checkpoint_sha=model_sha_by_owner[str(owner)], controller_sha=controller_sha_by_owner[owner])
        for owner in (0, 1)}
    model_sha = (model_sha_by_owner["0"] if model_sha_by_owner["0"] == model_sha_by_owner["1"]
        else semantic_digest(model_sha_by_owner))
    controller_sha = (controller_sha_by_owner[0] if controller_sha_by_owner[0] == controller_sha_by_owner[1]
        else semantic_digest({str(key): value for key, value in controller_sha_by_owner.items()}))
    limits = SearchLimits(root_noise=.25, root_noise_mode="training")
    search_sha = budget_search_sha(limits, simulations)
    runtime_sha = semantic_digest(dict(schema="sspi-mac-pilot-native-runtime.v1",
        engine="android-arm64-avd", deck=list(HOG_DECK), delay_ms=[200, 400, 600, 800, 1000],
        wait_ticks=20, wake_refills=False, refill_time="hog-observed-cadence.v1",
        ignored_early_play="wait-no-reward.v1", search_sha=search_sha, source_sha=_source_sha(),
        search_probability=search_probability, episode_kind=episode_kind,
        temporal_augmentation=TEMPORAL_SCHEMA if augmenter is not None else None,
        random_stream_id=seed_identity["random_stream_id"],
        actual_match_seed=seed_identity["actual_match_seed"],
        actual_rollout_seed=seed_identity["actual_rollout_seed"],
        native_transport=transport,
        belief_transport=belief_transport,
        actual_env_id=actual_env_id,
        native_ports=dict(actual=actual_port,
            belief_by_owner={str(owner): port for owner, port in belief_ports.items()})))
    identity = dict(run_id=run_id, arm=arm, game_id=game_id, seed=seed,
        **seed_identity,
        model_sha256=model_sha, controller_sha256=controller_sha,
        model_sha256_by_owner=model_sha_by_owner,
        controller_sha256_by_owner={str(key): value for key, value in controller_sha_by_owner.items()},
        search_protocol_sha256=search_sha, runtime_sha256=runtime_sha,
        source_sha256=_source_sha(), device=device, engine_sha256=None,
        episode_kind=episode_kind, search_probability=search_probability,
        native_transport=transport,
        belief_transport=belief_transport,
        actual_env_id=actual_env_id,
        native_ports=dict(actual=actual_port,
            belief_by_owner={str(owner): port for owner, port in belief_ports.items()}))

    actual_native = (EventBatchedSSPINative(host, actual_port, env_id=actual_env_id, timeout=90.,
                         **({"event_deltas": True} if transport == "event-json-delta" else {}))
        if transport in ("event-json", "event-json-delta")
        else SearchNative(host, actual_port, transport=transport, timeout=90.))
    environment = _make_environment(actual_native, bundle)
    rollout = None

    def make_belief_provider(owner, port, history):
        factory = HistoryBeliefFactory(
            lambda: make_belief_native(host, port, transport=belief_transport, timeout=90.),
            catalog=bundle.card_catalog, opponent=inference_by_owner[1 - owner],
            sidecar_factory=BattleEnvSidecar, rollout_factory=make_sspi_rollout)
        return SSPIHistorySearchProvider(factory, history=history,
            seed=split_seed(seed, "history-provider", run_id, arm,
                seed_identity["random_stream_id"], owner))

    belief_providers = OwnerBeliefProviders(belief_ports, make_belief_provider)
    roots, action_log = [], []
    counters = dict(decisions={"0": 0, "1": 0}, search={"0": 0, "1": 0},
        direct={"0": 0, "1": 0}, play={"0": 0, "1": 0}, wait={"0": 0, "1": 0},
        ability={"0": 0, "1": 0}, ready_hand_counts={"2": 0, "3": 0, "4": 0},
        refill_events=0, ignored_early_plays=0, groups=0)
    started = time.monotonic()
    phase_started = time.perf_counter()
    timing_profile = {}
    try:
        worker_attestations = preflight_native_endpoints(host, actual_port, belief_ports,
            transport=transport, actual_env_id=actual_env_id,
            ruleset_manifest=environment.ruleset_manifest,
            include_belief=episode_kind != "evaluation")
        identity["native_worker_attestations"] = worker_attestations
        identity["engine_sha256"] = worker_attestations["actual"]["libg_sha256"]
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK,
                seed=seed_identity["actual_match_seed"],
                end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=20, event_driven_decisions=True,
                allow_initial_remaining_runtime_rejections=True))
        if isinstance(actual_native, EventBatchedSSPINative):
            actual_native.start_atomic_steps(initial_tick=environment.current_tick)
        else:
            actual_native.start_atomic_steps()
        engine_sha = actual_native.attest().libg_sha256
        if engine_sha != identity["engine_sha256"]:
            raise ValueError("actual SSPI engine identity changed after endpoint preflight")
        identity["engine_sha256"] = engine_sha
        rollout = make_sspi_rollout(environment, catalog=bundle.card_catalog,
            execution_seed=seed_identity["actual_rollout_seed"])
        opening_information = rollout.freeze_due_information(timing_profile=timing_profile)
        if set(opening_information) != {0, 1}:
            raise ValueError("native opening did not freeze both players")
        histories = {} if episode_kind != "evaluation" else None
        for owner in (0, 1):
            information = opening_information[owner]
            validate_fair_view(rollout.runtime.frozen[owner], owner)
            if information.hand.ready_hand_count != 4 or information.legality.ability_candidates:
                raise ValueError("native Hog opening differs from the SSPI action contract")
            if histories is not None:
                histories[owner] = FairHistoryRecorder(rollout.runtime.frozen[owner], information,
                    opening_tick=environment.warmup_ticks)

        codec = ActionCodec(HOG_DECK)
        decision_indices = {0: 0, 1: 0}
        observations = None
        last_progress = started
        timing_profile["setup_seconds"] = time.perf_counter() - phase_started
        loop_started = time.perf_counter()

        while not rollout.runtime.ended and not rollout.runtime.truncated:
            with _timed(timing_profile, "freeze_seconds"):
                frozen = rollout.freeze_due_information(timing_profile=timing_profile)
            selected = {}
            source_roots = {}
            if frozen:
                group_started = time.perf_counter()
                if set(frozen) != rollout.runtime.clock.ready:
                    raise ValueError("frozen SSPI owners differ from the native same-tick ready set")
                group = dict(tick=rollout.runtime.clock.tick,
                    owners=sorted(frozen), fair_sha256={str(owner): frozen[owner].sha256
                        for owner in sorted(frozen)}, selections=[])
                # Both FAIR views are frozen before any gate, search, or action submission.
                for owner, information in sorted(frozen.items()):
                    index = decision_indices[owner]
                    counters["decisions"][str(owner)] += 1
                    hand_count = str(information.hand.ready_hand_count)
                    counters["ready_hand_counts"][hand_count] = counters["ready_hand_counts"].get(hand_count, 0) + 1
                    search_selected, gate_seed = decision_gate(seed, run_id=run_id, arm=arm,
                        game_id=seed_identity["random_stream_id"], owner=owner, decision_index=index,
                        probability=search_probability)
                    if search_selected:
                        with _timed(timing_profile, "history_capture_seconds"):
                            history = histories[owner].capture(information)
                            belief_provider = belief_providers.for_search(owner, history)
                        search_seed = split_seed(seed, "sspi-search.v1", run_id, arm,
                            seed_identity["random_stream_id"], owner, index)
                        behavior_seed = split_seed(seed, "sspi-search-behavior.v1", run_id, arm,
                            seed_identity["random_stream_id"], owner, index)
                        with _timed(timing_profile, "search_seconds"):
                            outcome = CompletedBoundarySearch(belief_provider,
                                inference_by_owner[0], inference_by_owner[1],
                                limits=limits).run(information, seed=search_seed,
                                    behavior_seed=behavior_seed, codec=codec,
                                    identity=dict(search_protocol_sha=search_sha,
                                        root_belief_prior_sha=belief_provider.sampler_sha,
                                        checkpoint_blue_sha=model_sha_by_owner["0"],
                                        checkpoint_red_sha=model_sha_by_owner["1"],
                                        controller_blue_sha=controller_sha_by_owner[0],
                                        controller_red_sha=controller_sha_by_owner[1]),
                                    mode="FULL", simulation_budget=simulations)
                        action = outcome.selected
                        with _timed(timing_profile, "root_record_seconds"):
                            visits = _root_visits(outcome, codec)
                            roots.append(dict(root_id=f"{game_id}:{len(roots):06d}", owner=owner,
                                tick=information.tick, decision_index=index, fair_sha256=information.sha256,
                                frame=pack(information.frame), hand=asdict(information.hand),
                                card_ids=information.legality.card_ids,
                                plays=information.legality.plays.detach().cpu().clone(),
                                pending_arrival_ticks=information.legality.pending_arrival_ticks,
                                wait=bool(information.legality.modes[ActionMode.WAIT]),
                                visits=visits, simulations=outcome.result["valid_simulations"],
                                search_result=outcome.result, search_audit=outcome.audit,
                                gate_seed=gate_seed, search_seed=search_seed, behavior_seed=behavior_seed,
                                history_sha256=history.sha256))
                        counters["search"][str(owner)] += 1
                        source_roots[owner] = roots[-1]
                        decision_mode = "SEARCH"
                        selection_seed = behavior_seed
                    else:
                        action_generator = torch.Generator(device="cpu").manual_seed(
                            split_seed(seed, "sspi-direct-action.v1", run_id, arm,
                                seed_identity["random_stream_id"], owner, index))
                        with _timed(timing_profile, "direct_predict_seconds"):
                            policy, _ = inference_by_owner[owner].predict(information)
                        with _timed(timing_profile, "direct_sample_seconds"):
                            action, _ = policy.sample(action_generator)
                        counters["direct"][str(owner)] += 1
                        decision_mode = "DIRECT"
                        selection_seed = split_seed(seed, "sspi-direct-action.v1", run_id, arm,
                            seed_identity["random_stream_id"], owner, index)
                    with _timed(timing_profile, "decision_record_seconds"):
                        action = _native_execution_action(action, mode=decision_mode)
                        if not information.legality.legal(action):
                            raise ValueError("SSPI actor selected an illegal native action")
                        executed_action = information.legality.execution_action(action)
                        if histories is not None:
                            histories[owner].record_decision(information, executed_action)
                        selected[owner] = action
                        counters["ignored_early_plays"] += int(executed_action != action)
                        kind = "WAIT" if executed_action.card_id is None else "PLAY"
                        counters[kind.lower()][str(owner)] += 1
                        group["selections"].append(dict(owner=owner, decision_index=index,
                            mode=decision_mode, gate_seed=gate_seed, selection_seed=selection_seed,
                            action=_action_key(action), executed_action=_action_key(executed_action)))
                        action_log.append(dict(group=len(action_log), tick=information.tick,
                            owner=owner, decision_index=index, fair_sha256=information.sha256,
                            mode=decision_mode, gate_seed=gate_seed, selection_seed=selection_seed,
                            action=_action_key(action), executed_action=_action_key(executed_action)))
                        decision_indices[owner] += 1
                with _timed(timing_profile, "commit_seconds"):
                    rollout.commit_simultaneous(selected)
                if augmenter is not None:
                    with _timed(timing_profile, "augmentation_register_seconds"):
                        for owner, root in source_roots.items():
                            augmenter.register(root, rollout.runtime.clock.pending[owner],
                                rollout.tensorizers[owner])
                counters["groups"] += 1
                if progress is not None:
                    progress(dict(stage="decisions_committed", game_id=game_id,
                        tick=group["tick"], owners=group["owners"], selections=group["selections"],
                        search_roots=len(roots), elapsed_seconds=time.monotonic() - started))
                timing_profile["decision_group_seconds"] = (
                    timing_profile.get("decision_group_seconds", 0.)
                    + time.perf_counter() - group_started)

            if not rollout.runtime.ended and not rollout.runtime.truncated:
                with _timed(timing_profile, "advance_prepare_seconds"):
                    before = {owner: set(player["cardId"] for player in
                        next(item for item in environment.raw_observation["players"]
                            if item["owner"] == owner)["hand"]) for owner in (0, 1)}
                    evidence_start = len(rollout.runtime.clock.evidence)
                with _timed(timing_profile, "runtime_advance_seconds"):
                    rollout.advance(timing_profile=timing_profile)
                with _timed(timing_profile, "history_update_seconds"):
                    observations = (_observe_histories(rollout, histories,
                        timing_profile=timing_profile) if histories is not None else None)
                if augmenter is not None:
                    if observations is None:
                        raise AssertionError("continuation samples require FAIR history observations")
                    with _timed(timing_profile, "augmentation_observe_seconds"):
                        augmenter.observe(environment=environment, observations=observations,
                            clock=rollout.runtime.clock, catalog=bundle.card_catalog,
                            evidence=rollout.runtime.clock.evidence[evidence_start:],
                            refill_clock=rollout.runtime.refill_observer)
                with _timed(timing_profile, "advance_bookkeeping_seconds"):
                    after = {owner: set(player["cardId"] for player in
                        next(item for item in environment.raw_observation["players"]
                            if item["owner"] == owner)["hand"]) for owner in (0, 1)}
                    counters["refill_events"] += sum(max(0, len(after[owner] - before[owner])) for owner in (0, 1))
                    if progress is not None and time.monotonic() - last_progress >= 60.:
                        progress(dict(stage="native_advance", game_id=game_id,
                            tick=rollout.runtime.clock.tick, search_roots=len(roots),
                            seconds_since_search_check=time.monotonic() - last_progress,
                            elapsed_seconds=time.monotonic() - started,
                            reservation_evidence=len(rollout.runtime.clock.evidence) - evidence_start))
                        last_progress = time.monotonic()

        timing_profile["loop_seconds"] = time.perf_counter() - loop_started
        finalize_started = time.perf_counter()
        if observations is None or observations[0].tick != rollout.runtime.clock.tick:
            with _timed(timing_profile, "terminal_fair_view_seconds"):
                observations = environment.observe_dual(policy_features_only=True)
        terminal = observations[0].terminal
        if observations[1].terminal != terminal:
            raise ValueError("native owner observations disagree on final WDL")
        result = terminal_result(terminal, environment.raw_observation,
            truncated=rollout.runtime.truncated, failed=rollout.runtime.failed)
        if (not rollout.runtime.ended or rollout.runtime.truncated or rollout.runtime.failed
                or rollout.runtime.clock.pending or rollout.runtime.clock.ready or rollout.runtime.frozen
                or rollout.runtime.clock.tick != result["terminal_tick"]
                or any(event["reason"] == "rejected" for event in rollout.runtime.clock.evidence)):
            raise ValueError("SSPI game did not reach a clean native terminal boundary")
        belief_providers.close()
        for root in roots:
            root["wdl"] = result["labels"][root["owner"]]
            for sample in root.get("continuations", ()):
                sample["wdl"] = root["wdl"]
        if augmenter is not None and augmenter.pending:
            raise ValueError("complete episode has unresolved continuation reservations")
        if any(semantic_digest(item.state_dict()) != model_sha_by_owner[str(owner)]
                for owner, item in models_by_owner.items()):
            raise ValueError("actor model weights changed during a frozen collection generation")

        native_performance = (actual_native.finish_capture_profile()
            if isinstance(actual_native, EventBatchedSSPINative) else None)
        tensor_path = destination / "episode.pt"
        with _timed(timing_profile, "tensor_write_seconds"):
            tensor_sha = _atomic_torch(tensor_path, dict(schema=EPISODE_SCHEMA,
                identity=identity, roots=roots))
        with _timed(timing_profile, "actions_write_seconds"):
            _atomic_json(destination / "actions.json", dict(schema="sspi-action-ledger.v1",
                rows=action_log, reservation_evidence=rollout.runtime.clock.evidence))
        timing_profile["finalize_seconds"] = time.perf_counter() - finalize_started
        timing_profile["group_other_seconds"] = max(0., timing_profile.get("decision_group_seconds", 0.)
            - sum(timing_profile.get(name, 0.) for name in (
                "history_capture_seconds", "search_seconds", "root_record_seconds",
                "direct_predict_seconds", "direct_sample_seconds", "decision_record_seconds",
                "commit_seconds", "augmentation_register_seconds")))
        timing_profile["runtime_other_seconds"] = max(0., timing_profile.get("runtime_advance_seconds", 0.)
            - sum(timing_profile.get(name, 0.) for name in (
                "step_transition_seconds", "step_observation_seconds",
                "runtime_fair_view_seconds", "refill_observer_seconds")))
        timing_profile["loop_other_seconds"] = max(0., timing_profile["loop_seconds"]
            - sum(timing_profile.get(name, 0.) for name in (
                "freeze_seconds", "decision_group_seconds", "advance_prepare_seconds",
                "runtime_advance_seconds", "history_update_seconds",
                "augmentation_observe_seconds", "advance_bookkeeping_seconds")))
        timing_profile["finalize_other_seconds"] = max(0., timing_profile["finalize_seconds"]
            - sum(timing_profile.get(name, 0.) for name in (
                "terminal_fair_view_seconds", "tensor_write_seconds", "actions_write_seconds")))
        timing_profile["accounted_seconds"] = sum(timing_profile[name]
            for name in ("setup_seconds", "loop_seconds", "finalize_seconds"))
        episode = dict(schema=EPISODE_SCHEMA, identity=identity, status="complete",
            episode_kind=episode_kind, training_admissible=training_admissible,
            result=result, tensor_file="episode.pt",
            tensor_sha256=tensor_sha, actions_file="actions.json", decisions=counters,
            actions_sha256=hashlib.sha256((destination / "actions.json").read_bytes()).hexdigest(),
            temporal_augmentation=augmenter.statistics() if augmenter is not None else None,
            native_advance_performance=dict(rollout.runtime.advance_profile),
            search_roots=len(roots), search_probability=search_probability, search_simulations=simulations,
            search_fraction_actual=(sum(map(int, counters["search"].values())) /
                max(1, sum(map(int, counters["decisions"].values())))),
            native_capture_performance=native_performance,
            timing_profile=dict(schema="sspi-collection-timing.v1", **timing_profile),
            elapsed_seconds=time.monotonic() - started,
            terminal_tick=rollout.runtime.clock.tick)
        _atomic_json(destination / "episode.json", episode)
        if progress is not None:
            progress(dict(stage="episode_complete", game_id=game_id, result=result,
                search_roots=len(roots), decisions=counters["decisions"],
                elapsed_seconds=episode["elapsed_seconds"], output=str(destination)))
        return episode
    except BaseException as error:
        try:
            _atomic_torch(destination / "partial.pt", dict(schema=EPISODE_SCHEMA,
                identity=identity, roots=roots, actions=action_log))
            _atomic_json(destination / "quarantine.json", dict(status="quarantined",
                training_admissible=False, exception=type(error).__name__, reason=str(error),
                truncated=bool(rollout and rollout.runtime.truncated),
                ended=bool(rollout and rollout.runtime.ended), elapsed_seconds=time.monotonic() - started))
        except BaseException:
            pass
        raise
    finally:
        belief_providers.close()
        if rollout is not None:
            rollout.runtime.frozen.clear()
            rollout.information.clear()
        actual_native.close_transport()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Collect one frozen-model SSPI native Hog episode")
    parser.add_argument("--actual-port", type=int, required=True)
    parser.add_argument("--actual-env-id", type=int, default=0)
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--belief-port-owner-1", type=int)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--arm", choices=tuple(ARM_CONFIG), default="A_5PCT_128")
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--random-stream-id")
    parser.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    parser.add_argument("--transport", choices=("legacy", "persistent", "event-json", "event-json-delta"), default="legacy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-temporal-augmentation", action="store_true",
        help="Keep the original SEARCH-root-only baseline without continuation samples")
    args = parser.parse_args()
    def progress(row):
        print(json.dumps(row, sort_keys=True), flush=True)
    result = collect_episode(actual_port=args.actual_port, actual_env_id=args.actual_env_id,
        belief_port=args.belief_port,
        output_dir=args.output_dir, run_id=args.run_id, arm=args.arm,
        game_id=args.game_id, seed=args.seed, device=args.device, host=args.host,
        random_stream_id=args.random_stream_id,
        transport=args.transport, progress=progress,
        temporal_augmentation=not args.no_temporal_augmentation,
        belief_ports=None if args.belief_port_owner_1 is None else
            {0: args.belief_port, 1: args.belief_port_owner_1})
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
