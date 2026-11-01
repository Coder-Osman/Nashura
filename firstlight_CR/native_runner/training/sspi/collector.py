"""Single-actor SSPI self-play collection on the FAIR native rollout runtime."""

from dataclasses import asdict
from collections.abc import Mapping
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

from native_runner.cr_native_env import NativeClashEnv
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
from ..fair_final.codec import ActionCodec
from ..fair_final.episode import terminal_result
from ..fair_final.history import FairHistoryRecorder
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
from .protocol import ActionMode, SSPIAction, from_full_action, to_full_action
from .rollout import make_sspi_rollout
from .search import budget_search_sha, make_search_inference, SSPIHistorySearchProvider

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "research" / "puct-runtime"))
from native_adapter import BattleEnvSidecar


ARM_CONFIG = {
    "A_5PCT_128": (0.05, 128),
}
EPISODE_SCHEMA = "sspi-mac-pilot-episode.v1"


class SearchNative(NativeClashEnv):
    """Use the selected, already-verified control transport for an AVD worker."""

    def __init__(self, host, port, *, transport="legacy", timeout=90.):
        if transport not in ("legacy", "persistent"):
            raise ValueError("SSPI native transport must be legacy or persistent")
        super().__init__(host, port, timeout=timeout)
        self.transport = transport
        self._persistent_transport_requested = transport == "persistent"

    def start_atomic_steps(self):
        return None


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


def preflight_native_endpoints(host, actual_port, belief_ports, *, transport, ruleset_manifest=None):
    endpoints = [("actual", actual_port),
        ("belief_owner_0", belief_ports[0]), ("belief_owner_1", belief_ports[1])]
    attestations = {}
    expected_identity = None
    for role, port in endpoints:
        native = SearchNative(host, port, transport=transport, timeout=90.)
        try:
            status = native.status()
            if status.get("ready") is not True:
                raise ValueError(f"SSPI native {role} endpoint is not ready on port {port}")
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


def _observe_histories(rollout, histories):
    views = rollout.runtime.environment.observe_dual(policy_features_only=True)
    for owner in (0, 1):
        validate_fair_view(views[owner], owner)
        histories[owner].observe_tick(views[owner])
    return views


def collect_episode(*, actual_port, belief_port, output_dir, run_id, arm, game_id,
        seed=20261101, model=None, models_by_owner=None, device="cpu", host="127.0.0.1",
        transport="legacy", search_probability=None, episode_kind="training", progress=None,
        temporal_augmentation=True, random_stream_id=None, belief_ports=None):
    """Collect one complete native match; incomplete matches are quarantined."""
    try:
        belief_ports = resolve_belief_ports(actual_port, belief_port, belief_ports)
    except ValueError:
        raise
    if (arm not in ARM_CONFIG or type(seed) is not int or seed < 0
            or transport not in ("legacy", "persistent")):
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
    controller_sha_by_owner = {owner: semantic_digest(dict(model_sha=model_sha_by_owner[str(owner)],
        action_schema="sspi-tagged-action.v1", delay_ms=[200, 400, 600, 800, 1000],
        wait_ticks=20, device=device, deck=list(HOG_DECK), native_transport=transport))
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
        wait_ticks=20, wake_refills=True, search_sha=search_sha, source_sha=_source_sha(),
        search_probability=search_probability, episode_kind=episode_kind,
        temporal_augmentation=TEMPORAL_SCHEMA if augmenter is not None else None,
        random_stream_id=seed_identity["random_stream_id"],
        actual_match_seed=seed_identity["actual_match_seed"],
        actual_rollout_seed=seed_identity["actual_rollout_seed"],
        native_transport=transport,
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
        native_ports=dict(actual=actual_port,
            belief_by_owner={str(owner): port for owner, port in belief_ports.items()}))

    actual_native = SearchNative(host, actual_port, transport=transport, timeout=90.)
    environment = _make_environment(actual_native, bundle)
    rollout = None
    def make_belief_provider(owner, port, history):
        factory = HistoryBeliefFactory(
            lambda: SearchNative(host, port, transport=transport, timeout=90.),
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
        refill_events=0, groups=0)
    started = time.monotonic()
    try:
        worker_attestations = preflight_native_endpoints(host, actual_port, belief_ports,
            transport=transport, ruleset_manifest=environment.ruleset_manifest)
        identity["native_worker_attestations"] = worker_attestations
        identity["engine_sha256"] = worker_attestations["actual"]["libg_sha256"]
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK,
                seed=seed_identity["actual_match_seed"],
                end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        actual_native.start_atomic_steps()
        engine_sha = actual_native.attest().libg_sha256
        if engine_sha != identity["engine_sha256"]:
            raise ValueError("actual SSPI engine identity changed after endpoint preflight")
        identity["engine_sha256"] = engine_sha
        rollout = make_sspi_rollout(environment, catalog=bundle.card_catalog,
            execution_seed=seed_identity["actual_rollout_seed"])
        opening_information = rollout.freeze_due_information()
        if set(opening_information) != {0, 1}:
            raise ValueError("native opening did not freeze both players")
        histories = {}
        for owner in (0, 1):
            information = opening_information[owner]
            validate_fair_view(rollout.runtime.frozen[owner], owner)
            if information.hand.ready_hand_count != 4 or information.legality.ability_candidates:
                raise ValueError("native Hog opening differs from the SSPI action contract")
            histories[owner] = FairHistoryRecorder(rollout.runtime.frozen[owner], information,
                opening_tick=environment.warmup_ticks)

        codec = ActionCodec(HOG_DECK)
        decision_indices = {0: 0, 1: 0}
        previous_hands = {owner: set(information.hand.ready_card_ids)
            for owner, information in opening_information.items()}
        last_progress = started

        while not rollout.runtime.ended and not rollout.runtime.truncated:
            frozen = rollout.freeze_due_information()
            selected = {}
            source_roots = {}
            if frozen:
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
                        history = histories[owner].capture(information)
                        belief_provider = belief_providers.for_search(owner, history)
                        search_seed = split_seed(seed, "sspi-search.v1", run_id, arm,
                            seed_identity["random_stream_id"], owner, index)
                        behavior_seed = split_seed(seed, "sspi-search-behavior.v1", run_id, arm,
                            seed_identity["random_stream_id"], owner, index)
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
                        visits = _root_visits(outcome, codec)
                        roots.append(dict(root_id=f"{game_id}:{len(roots):06d}", owner=owner,
                            tick=information.tick, decision_index=index, fair_sha256=information.sha256,
                            frame=pack(information.frame), hand=asdict(information.hand),
                            card_ids=information.legality.card_ids,
                            plays=information.legality.plays.detach().cpu().clone(),
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
                        policy, _ = inference_by_owner[owner].predict(information)
                        action, _ = policy.sample(action_generator)
                        counters["direct"][str(owner)] += 1
                        decision_mode = "DIRECT"
                        selection_seed = split_seed(seed, "sspi-direct-action.v1", run_id, arm,
                            seed_identity["random_stream_id"], owner, index)
                    action = _native_execution_action(action, mode=decision_mode)
                    if not information.legality.legal(action):
                        raise ValueError("SSPI actor selected an illegal native action")
                    histories[owner].record_decision(information, action)
                    selected[owner] = action
                    kind = "WAIT" if action.card_id is None else "PLAY"
                    counters[kind.lower()][str(owner)] += 1
                    group["selections"].append(dict(owner=owner, decision_index=index,
                        mode=decision_mode, gate_seed=gate_seed, selection_seed=selection_seed,
                        action=_action_key(action)))
                    action_log.append(dict(group=len(action_log), tick=information.tick,
                        owner=owner, decision_index=index, fair_sha256=information.sha256,
                        mode=decision_mode, gate_seed=gate_seed, selection_seed=selection_seed,
                        action=_action_key(action)))
                    decision_indices[owner] += 1
                rollout.commit_simultaneous(selected)
                if augmenter is not None:
                    for owner, root in source_roots.items():
                        augmenter.register(root, rollout.runtime.clock.pending[owner],
                            rollout.tensorizers[owner])
                counters["groups"] += 1
                if progress is not None:
                    progress(dict(stage="decisions_committed", game_id=game_id,
                        tick=group["tick"], owners=group["owners"], selections=group["selections"],
                        search_roots=len(roots), elapsed_seconds=time.monotonic() - started))

            if not rollout.runtime.ended and not rollout.runtime.truncated:
                before = {owner: set(player["cardId"] for player in
                    next(item for item in environment.raw_observation["players"]
                        if item["owner"] == owner)["hand"]) for owner in (0, 1)}
                evidence_start = len(rollout.runtime.clock.evidence)
                rollout.advance()
                observations = _observe_histories(rollout, histories)
                if augmenter is not None:
                    augmenter.observe(environment=environment, observations=observations,
                        clock=rollout.runtime.clock, catalog=bundle.card_catalog,
                        evidence=rollout.runtime.clock.evidence[evidence_start:])
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

        tensor_path = destination / "episode.pt"
        tensor_sha = _atomic_torch(tensor_path, dict(schema=EPISODE_SCHEMA,
            identity=identity, roots=roots))
        _atomic_json(destination / "actions.json", dict(schema="sspi-action-ledger.v1",
            rows=action_log, reservation_evidence=rollout.runtime.clock.evidence))
        episode = dict(schema=EPISODE_SCHEMA, identity=identity, status="complete",
            episode_kind=episode_kind, training_admissible=training_admissible,
            result=result, tensor_file="episode.pt",
            tensor_sha256=tensor_sha, actions_file="actions.json", decisions=counters,
            actions_sha256=hashlib.sha256((destination / "actions.json").read_bytes()).hexdigest(),
            temporal_augmentation=augmenter.statistics() if augmenter is not None else None,
            search_roots=len(roots), search_probability=search_probability, search_simulations=simulations,
            search_fraction_actual=(sum(map(int, counters["search"].values())) /
                max(1, sum(map(int, counters["decisions"].values())))),
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
    parser.add_argument("--belief-port", type=int, required=True)
    parser.add_argument("--belief-port-owner-1", type=int)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--arm", choices=tuple(ARM_CONFIG), default="A_5PCT_128")
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--random-stream-id")
    parser.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    parser.add_argument("--transport", choices=("legacy", "persistent"), default="legacy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-temporal-augmentation", action="store_true",
        help="Keep the original SEARCH-root-only baseline without continuation samples")
    args = parser.parse_args()
    def progress(row):
        print(json.dumps(row, sort_keys=True), flush=True)
    result = collect_episode(actual_port=args.actual_port, belief_port=args.belief_port,
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
