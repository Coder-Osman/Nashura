"""Three-specialist league trainer (event-driven decisions).

Reward is **win/loss only**: r_T in {+1,-1,0} and zero elsewhere. Potential
shaping, elixir-overflow penalty and per-play bonus all default to off, and
episodes run to the natural end (MATCH_TICKS) so the terminal result exists.
Anchors and league composition: tools/league_decks.py (see RL_LEAGUE_* for the
RL trio); design: SELFPLAY_RL_ARCHITECTURE.md.

Worker (per engine): drives resident lanes with matchup-scheduled decks,
records per-decision (observation fp16 storage, sampled action, behavior
log-prob/value, reward) per policy, and reloads policy checkpoints between
episodes.
Trainer: consumes finished match records per policy, assembles
StoredRolloutV4 lanes, runs distributed_ppo_update_v4, saves checkpoints, and
logs the evolving win-rate matrix from match results.
Master: gates engine readiness, spawns workers + trainer, enforces STOP.

Usage:
    .venv/bin/python tools/league_train.py --base-checkpoint <ckpt> \
        --output runs/league --engines 0,1,...
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.league_decks import LEAGUE_DECKS, LEAGUE_MATCHUPS, validate_league_decks  # noqa: E402
from tools.selfplay_resident import (  # noqa: E402
    ADB,
    ENGINES_PER_INSTANCE,
    INSTANCES,
    PACKAGE,
    RESET_OPTIONS,
    control,
    restart_instance_engines,
)

POLICY_NAMES = tuple(LEAGUE_DECKS)
# Retained for the recorded 1x profile (90 warm-up + 2400 single-elixir ticks).
ONE_X_MAX_TICKS = 2490
ELIXIR_PER_TICK_AT_1X = 1.0 / 56.0  # one elixir per 2.8 s at 20 Hz
# Episode length. The reward is win/loss only, so the episode must run to the
# natural end: natives finalize at 7200 ticks, and a truncated episode reports
# result (0, 0) -- i.e. zero reward on every step, which teaches nothing.
MATCH_TICKS = int(os.environ.get("MATCH_TICKS", "7200"))


def policy_checkpoint_path(policy_dir: Path, policy: str) -> Path:
    return policy_dir / f"{policy}.pt"


def worker() -> int:
    engine_index = int(os.environ["ENGINE_INDEX"])
    port = int(os.environ["HOST_PORT"])
    lanes_requested = int(os.environ.get("LANES", "6"))
    rollout_dir = Path(os.environ["ROLLOUT_DIR"])
    policy_dir = Path(os.environ["POLICY_DIR"])
    base_checkpoint = os.environ["BASE_CHECKPOINT"]
    stop_file = Path(os.environ["STOP_FILE"])
    # Reward is the terminal result plus a margin term: potential shaping,
    # elixir overflow and the per-play bonus are off by default so the only
    # extra signal is how the match was won (crowns / tower damage), not what
    # happened on the way there. Set the env vars to reproduce the old
    # hand-tuned profile.
    shaping_beta = float(os.environ.get("SHAPING_BETA", "0.0"))
    overflow_beta = float(os.environ.get("OVERFLOW_BETA", "0.0"))
    reward_play = float(os.environ.get("REWARD_PLAY", "0.0"))
    terminal_margin_beta = float(os.environ.get("TERMINAL_MARGIN_BETA", "0.05"))
    explore_gate_temperature = float(os.environ.get("EXPLORE_GATE_TEMPERATURE", "1.0"))
    force_act_probability = float(os.environ.get("FORCE_ACT_PROBABILITY", "0.0"))

    import torch

    torch.set_num_threads(1)
    from native_runner.battle_env import BattleEnvV1
    from native_runner.contracts import ActionKind, ActionV1
    from native_runner.cr_native_env import ResidentNativeClashEnv
    from native_runner.match_factory import MatchConfig, tournament_level_kwargs
    from native_runner.resident_batch_vector import ResidentBatchCoordinatorV1, ResidentBatchNativeProxyV1
    from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
    from native_runner.training.v4.decoding import decode_action_sequence_v4
    from native_runner.training.v4.expert import FIRST_POLICY_DECISION_TICK, POLICY_DECISION_TICKS
    from native_runner.training.v4.factory import (
        build_episode_tensorizer_v4,
        production_semantic_bundle,
    )
    from native_runner.training.v4.policy_session import load_policy_v4
    from native_runner.training.v4.tensors import (
        RecurrentPolicyStateV4,
        concatenate_padded_tensor_records,
        concatenate_tensor_records,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    bundle = production_semantic_bundle()

    models: dict[str, object] = {}
    checkpoint_stamps: dict[str, float] = {}

    def load_models() -> None:
        for policy in POLICY_NAMES:
            path = policy_checkpoint_path(policy_dir, policy)
            source = str(path) if path.exists() else base_checkpoint
            model = load_policy_v4(source, device=device).model
            # Exploration: the checkpoint froze gate_temperature near 0.2, which
            # drives the gate toward deterministic WAIT and starves ACT data.
            # Raise it here so rollout keeps exploring ACT (the worker only
            # produces data; the authoritative trainer owns the saved value).
            model.set_ppo_gate_temperature(explore_gate_temperature)
            models[policy] = model
            checkpoint_stamps[policy] = path.stat().st_mtime if path.exists() else 0.0

    load_models()
    natives = tuple(
        ResidentNativeClashEnv(env_id, "127.0.0.1", port, timeout=90.0) for env_id in range(lanes_requested)
    )
    coordinator = ResidentBatchCoordinatorV1(
        natives, timeout=90.0, training_capture=True, compressed_json=True, validate_json_capture=False
    )
    proxies = tuple(ResidentBatchNativeProxyV1(native, coordinator) for native in natives)
    environments = tuple(
        BattleEnvV1(
            native=proxy,
            card_catalog=bundle.native_card_catalog,
            semantic_subset_contract=bundle.semantic_subset_contract,
            effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
            max_battle_ticks=MATCH_TICKS,
            shaping_beta=shaping_beta,
            princess_only_potential=False,
            truncation_terminal_potential=False,
            terminal_margin_beta=terminal_margin_beta,
        )
        for proxy in proxies
    )
    executor = ThreadPoolExecutor(max_workers=lanes_requested, thread_name_prefix=f"league-e{engine_index}")
    progress_path = rollout_dir / f"progress-e{engine_index:02d}.json"

    def progress(**data):
        data.update({"engine": engine_index, "wall": time.time()})
        progress_path.write_text(json.dumps(data))

    @dataclass
    class ActorSlot:
        policy: str
        tensorizer: object
        state: object
        first_decision: bool = True
        nonwait: int = 0

    @dataclass
    class MatchLane:
        env_id: int
        matchup: tuple[str, str]
        environment: object
        actors: list
        observations: dict
        records: list  # per owner: dict(observations=[], actions=[], log_prob=[], value=[], reward=[], terminated=[], truncated=[])
        elixir_before: list = None

        def __post_init__(self) -> None:
            self.elixir_before = [0.0, 0.0]

    def decide_and_command(lanes) -> tuple[dict[int, dict[int, tuple]], set[int], dict[int, list[float]]]:
        ready: list[tuple] = []
        for lane in lanes:
            for owner in (0, 1):
                actor = lane.actors[owner]
                observation = lane.observations[owner]
                if observation.tick >= FIRST_POLICY_DECISION_TICK:
                    ready.append((lane, owner, actor))
                else:
                    actor.tensorizer.tensorize(observation, validate=False)
        commands: dict[int, dict[int, tuple]] = {}
        recorded: set[int] = set()
        played: dict[int, list[bool]] = {lane.env_id: [False, False] for lane in lanes}
        spent_this_round: dict[int, list[float]] = {lane.env_id: [0.0, 0.0] for lane in lanes}
        by_policy: dict[str, list] = {}
        for entry in ready:
            by_policy.setdefault(entry[2].policy, []).append(entry)
        for policy, entries in by_policy.items():
            model = models[policy]
            with torch.inference_mode():
                batches = tuple(
                    actor.tensorizer.tensorize(lane.observations[owner], validate=False)
                    for lane, owner, actor in entries
                )
                combined = concatenate_padded_tensor_records(batches)
                stacked = RecurrentPolicyStateV4(
                    hidden=torch.cat(tuple(actor.state.hidden for _l, _o, actor in entries)),
                    cell=torch.cat(tuple(actor.state.cell for _l, _o, actor in entries)),
                )
                episode_start = torch.tensor(
                    [actor.first_decision for _l, _o, actor in entries], dtype=torch.bool, device=device
                )
                model_batch = combined.to_model_input(device)
                context = model.forward(model_batch, stacked, episode_start=episode_start, validate=False)
                free = model._select_action(combined, context, sample=True, validate=False)
                # Forced-ACT exploration, OFF by default (force_act_probability
                # 0.0). It was added to keep the recurrent gate from collapsing
                # to deterministic WAIT, but forcing actions papers over the
                # cause: under win/loss-only reward the fix is exploration
                # (entropy, sampling temperature) plus a value head that
                # actually predicts the outcome. Raise the probability only to
                # reproduce the old 1x-league configuration.
                forced_rows = [
                    (
                        random.random() < force_act_probability
                        and (
                            bool(lane.observations[owner].action_mask.kinds.get(ActionKind.PLAY_CARD.value, False))
                            or bool(
                                lane.observations[owner].action_mask.kinds.get(
                                    ActionKind.ACTIVATE_ABILITY.value, False
                                )
                            )
                        )
                    )
                    for lane, owner, _actor in entries
                ]
                forced_mask = torch.tensor(forced_rows, dtype=torch.bool, device=device)
                if bool(forced_mask.any()):
                    forced = model._select_action(combined, context, sample=True, validate=False, preselected_act=True)
                    # Never poison the rollout with -inf: if a "legal" row
                    # still resolves to an illegal forced ACT, drop the force.
                    forced_mask = forced_mask & torch.isfinite(forced.log_prob)

                if bool(forced_mask.any()):

                    def merge(free_t, forced_t):
                        mask = forced_mask if free_t.dim() == 1 else forced_mask.view(-1, *([1] * (free_t.dim() - 1)))
                        return torch.where(mask, forced_t, free_t)

                    from native_runner.training.v4.tensors import ActionSequenceV4 as _ActionSequenceV4

                    output_actions = _ActionSequenceV4(
                        gate=merge(free.actions.gate, forced.actions.gate),
                        micro_action_count=merge(free.actions.micro_action_count, forced.actions.micro_action_count),
                        candidate_index=merge(free.actions.candidate_index, forced.actions.candidate_index),
                        candidate_uid=merge(free.actions.candidate_uid, forced.actions.candidate_uid),
                        target_cell=merge(free.actions.target_cell, forced.actions.target_cell),
                        delay_offset_bin=merge(free.actions.delay_offset_bin, forced.actions.delay_offset_bin),
                    )
                    output_next_state = RecurrentPolicyStateV4(
                        hidden=merge(free.next_state.hidden, forced.next_state.hidden),
                        cell=merge(free.next_state.cell, forced.next_state.cell),
                    )
                    forced_lp = forced.log_prob.detach().to("cpu")
                    forced_gate_lp = (
                        forced.action_components.gate_log_prob.detach().to("cpu")
                        if forced.action_components is not None
                        else torch.zeros_like(forced_lp)
                    )
                else:
                    output_actions = free.actions
                    output_next_state = free.next_state
                    forced_lp = None
                    forced_gate_lp = None
                log_probs = free.log_prob.detach().to("cpu")
                values = free.value.detach().to("cpu")
                forced_cpu = forced_mask.to("cpu")
                for row, ((lane, owner, actor), host_batch) in enumerate(zip(entries, batches)):
                    is_forced = bool(forced_cpu[row])
                    action = output_actions.narrow_batch(row, 1)
                    actor.tensorizer.record_action(action, host_batch, row=0, validate=False)
                    decoded = decode_action_sequence_v4(
                        action,
                        host_batch.candidates,
                        row=0,
                        observation=lane.observations[owner],
                        catalog=actor.tensorizer.catalog,
                        deck=actor.tensorizer.deck,
                        card_costs=actor.tensorizer.card_costs,
                        ability_id_by_vocab_id=actor.tensorizer.ability_id_by_vocab_id,
                        horizontal_mirror=actor.tensorizer.perspective.horizontal_mirror,
                        hand_slot_permutation=actor.tensorizer.perspective.hand_slot_permutation,
                        config=actor.tensorizer.config,
                        base_latency_ticks=1,
                        base_latency_ms=0.0,
                        validate=False,
                    )
                    actor.state = output_next_state.narrow_batch(row, 1)
                    actor.first_decision = False
                    chosen = tuple(decoded.actions)
                    actor.nonwait += sum(item.kind != ActionKind.WAIT for item in chosen)
                    played[lane.env_id][owner] = any(item.kind != ActionKind.WAIT for item in chosen)
                    record = lane.records[owner]
                    record["observations"].append(host_batch.to_storage("cpu", float_dtype=torch.float16))
                    record["actions"].append(action.to_storage("cpu"))
                    if is_forced:
                        # The PPO evaluator removes the gate component from
                        # forced frames (conditioned_gate_mask), so record the
                        # downstream-only log-prob to keep the ratio exact.
                        behavior_lp = float(forced_lp[row]) - float(forced_gate_lp[row])
                        record["log_prob"].append(behavior_lp)
                    else:
                        record["log_prob"].append(float(log_probs[row]))
                    record["forced"].append(is_forced)
                    record["value"].append(float(values[row]))
                    record["episode_start"].append(len(record["observations"]) == 1)
                    spent_this_round[lane.env_id][owner] = sum(
                        float(actor.tensorizer.card_costs.get(getattr(item, "card_id", 0), 0.0))
                        for item in chosen
                        if item.kind == ActionKind.PLAY_CARD
                    )
                    lane.elixir_before[owner] = float(
                        next(p.elixir_exact for p in lane.observations[owner].players if p.owner == owner)
                    )
                    recorded.add(lane.env_id)
                    commands.setdefault(lane.env_id, {})[owner] = chosen
        for lane in lanes:
            lane_commands = commands.get(lane.env_id, {})
            for owner in (0, 1):
                lane_commands.setdefault(owner, (ActionV1.wait(owner, ticks=POLICY_DECISION_TICKS),))
            commands[lane.env_id] = lane_commands
        return commands, recorded, spent_this_round, played

    episode = 0
    matches_done = 0
    try:
        while not stop_file.exists():
            # Hot-reload any policy checkpoints the trainer published.
            for policy in POLICY_NAMES:
                path = policy_checkpoint_path(policy_dir, policy)
                if path.exists() and path.stat().st_mtime > checkpoint_stamps[policy]:
                    models[policy] = load_policy_v4(str(path), device=device).model
                    checkpoint_stamps[policy] = path.stat().st_mtime

            coordinator.close_session()
            lanes = []
            for env_id in range(lanes_requested):
                matchup = LEAGUE_MATCHUPS[env_id % len(LEAGUE_MATCHUPS)]
                observations, _info = environments[env_id].reset(
                    match_config=MatchConfig(
                        deck0=list(LEAGUE_DECKS[matchup[0]]),
                        deck1=list(LEAGUE_DECKS[matchup[1]]),
                        seed=3_000_000 + engine_index * 1_000 + episode * 16 + env_id,
                        end_tick=MATCH_TICKS,
                        **tournament_level_kwargs(),
                    ),
                    options={**RESET_OPTIONS, "decision_ticks": POLICY_DECISION_TICKS},
                )
                initial_elixir = {
                    owner: next(p.elixir_exact for p in observations[owner].players if p.owner == owner)
                    for owner in (0, 1)
                }
                actors = []
                for owner in (0, 1):
                    policy = matchup[owner]
                    tensorizer = build_episode_tensorizer_v4(
                        environments[env_id].episode_config, actor_owner=owner
                    )
                    tensorizer.start_episode(observations[owner], initial_elixir=initial_elixir)
                    actors.append(
                        ActorSlot(
                            policy=policy,
                            tensorizer=tensorizer,
                            state=models[policy].initial_state(1, device=device),
                        )
                    )
                lanes.append(
                    MatchLane(
                        env_id=env_id,
                        matchup=matchup,
                        environment=environments[env_id],
                        actors=actors,
                        observations=observations,
                        records=[
                            {
                                "observations": [],
                                "actions": [],
                                "log_prob": [],
                                "value": [],
                                "reward": [],
                                "terminated": [],
                                "truncated": [],
                                "episode_start": [],
                                "forced": [],
                            }
                            for _owner in (0, 1)
                        ],
                    )
                )

            coordinator.start_session()
            active = list(range(lanes_requested))
            while active and not stop_file.exists():
                active_lanes = [lanes[index] for index in active]
                commands, recorded, spent, played = decide_and_command(active_lanes)
                coordinator.begin_round(tuple(lanes[index].env_id for index in active))
                futures = {
                    lanes[index].env_id: executor.submit(
                        lanes[index].environment.step,
                        commands[lanes[index].env_id],
                        record_trace=False,
                        policy_features_only=True,
                    )
                    for index in active
                }
                outcomes = {}
                for env_id, future in futures.items():
                    try:
                        outcomes[env_id] = future.result()
                    except BaseException as error:  # noqa: BLE001
                        outcomes[env_id] = error
                native_ok = coordinator.round_succeeded
                finish_error = None
                try:
                    coordinator.finish_round()
                except BaseException as error:  # noqa: BLE001
                    finish_error = error
                if not native_ok or finish_error is not None:
                    raise finish_error or RuntimeError("league batch round failed")
                for index in active[:]:
                    lane = lanes[index]
                    outcome = outcomes[lane.env_id]
                    if isinstance(outcome, BaseException):
                        active.remove(index)
                        continue
                    observations, rewards, terms, truncs, _infos = outcome
                    lane.observations = observations
                    ended = bool(terms["__all__"]) or bool(truncs["__all__"])
                    if lane.env_id in recorded:
                        multiplier = float(observations[0].time.elixir_multiplier)
                        generated = POLICY_DECISION_TICKS * ELIXIR_PER_TICK_AT_1X * multiplier
                        for owner in (0, 1):
                            record = lane.records[owner]
                            after = float(
                                next(p.elixir_exact for p in observations[owner].players if p.owner == owner)
                            )
                            waste = max(0.0, lane.elixir_before[owner] + generated - spent[lane.env_id][owner] - after)
                            deploy_bonus = float(reward_play) if played[lane.env_id][owner] else 0.0
                            # rewards[owner] is the env's win/loss result (+1/-1/0)
                            # at the terminal step and 0 otherwise -- with shaping,
                            # overflow and play bonuses defaulting to zero this
                            # expression is the whole reward.
                            record["reward"].append(
                                float(rewards[owner]) - overflow_beta * waste + deploy_bonus
                            )
                            record["terminated"].append(bool(terms["__all__"]))
                            record["truncated"].append(bool(truncs["__all__"]))
                    if ended:
                        active.remove(index)
                        terminal = observations[0].terminal
                        crowns = {p.owner: int(p.crowns) for p in observations[0].players}
                        for owner in (0, 1):
                            record = lane.records[owner]
                            if not record["observations"]:
                                continue
                            # Pack the whole match into one flat record: a few
                            # hundred tensors instead of ~100k per-frame
                            # dataclass objects, so trainer loads take ~0.1 s
                            # per match instead of ~3 s.
                            packed_observations = concatenate_padded_tensor_records(
                                tuple(record["observations"])
                            )
                            packed_actions = concatenate_tensor_records(tuple(record["actions"]))
                            payload = {
                                "schema": 2,
                                "policy": lane.actors[owner].policy,
                                "opponent_policy": lane.actors[1 - owner].policy,
                                "engine": engine_index,
                                "episode": episode,
                                "lane": lane.env_id,
                                "owner": owner,
                                "winner": terminal.winner,
                                "reason": terminal.reason,
                                "truncated": bool(truncs["__all__"]),
                                "tick": int(observations[0].tick),
                                "crowns": crowns,
                                "nonwait": lane.actors[owner].nonwait,
                                "frames": len(record["observations"]),
                                "packed_observations": packed_observations,
                                "packed_actions": packed_actions,
                                "log_prob": torch.tensor(record["log_prob"], dtype=torch.float32),
                                "value": torch.tensor(record["value"], dtype=torch.float32),
                                "reward": torch.tensor(record["reward"], dtype=torch.float32),
                                "terminated": torch.tensor(record["terminated"], dtype=torch.bool),
                                "truncated_rows": torch.tensor(record["truncated"], dtype=torch.bool),
                                "episode_start": torch.tensor(record["episode_start"], dtype=torch.bool),
                                "forced_gate": torch.tensor(record["forced"], dtype=torch.bool),
                            }
                            policy_dir_name = rollout_dir / lane.actors[owner].policy
                            policy_dir_name.mkdir(parents=True, exist_ok=True)
                            target_path = (
                                policy_dir_name
                                / f"m-e{engine_index:02d}-ep{episode:05d}-l{lane.env_id}-o{owner}.pt"
                            )
                            temporary_path = target_path.with_name(
                                f".{target_path.name}.{os.getpid()}.tmp"
                            )
                            torch.save(payload, temporary_path)
                            os.replace(temporary_path, target_path)
                        matches_done += 1
            coordinator.close_session()
            episode += 1
            progress(episode=episode, matches=matches_done)
    finally:
        try:
            coordinator.close_session()
        except Exception:  # noqa: BLE001
            pass
        executor.shutdown(wait=True)
        progress(episode=episode, matches=matches_done, done=True)
    return 0


def _normalize_record(payload: dict) -> dict:
    """Unify schema-2 packed records with legacy per-frame payloads."""

    if payload.get("schema") != 2:
        return payload
    frames = int(payload["frames"])
    observations = payload["packed_observations"]
    actions = payload["packed_actions"]
    forced_gate = payload.get("forced_gate")
    return {
        **{key: value for key, value in payload.items() if key not in {
            "schema", "packed_observations", "packed_actions",
            "log_prob", "value", "reward", "terminated", "truncated_rows", "episode_start", "forced_gate",
        }},
        "observations": [observations.narrow_batch(step, 1) for step in range(frames)],
        "actions": [actions.narrow_batch(step, 1) for step in range(frames)],
        "log_prob": payload["log_prob"].tolist(),
        "value": payload["value"].tolist(),
        "reward": payload["reward"].tolist(),
        "terminated": payload["terminated"].tolist(),
        "truncated": payload["truncated_rows"].tolist(),
        "episode_start": payload["episode_start"].tolist(),
        "forced": ([bool(value) for value in forced_gate.tolist()] if forced_gate is not None else [False] * frames),
    }


def assemble_rollout(records: list[dict]) -> dict:
    """Pad per-owner match records into [T, B] arrays for StoredRolloutV4.

    Padded steps repeat the final observation/action (mirroring the author's
    collate convention) and carry zero rewards, log-probs, and validity.
    """

    import torch

    from native_runner.training.v4.tensors import (
        concatenate_padded_tensor_records,
        concatenate_tensor_records,
    )

    lengths = [len(item["observations"]) for item in records]
    time_steps = max(lengths)
    batch = len(records)
    observations = []
    actions = []
    for step in range(time_steps):
        step_observations = []
        step_actions = []
        for lane, length in zip(records, lengths):
            index = min(step, length - 1)
            step_observations.append(lane["observations"][index])
            step_actions.append(lane["actions"][index])
        observations.append(concatenate_padded_tensor_records(tuple(step_observations)))
        actions.append(concatenate_tensor_records(tuple(step_actions)))

    def column(field: str, fill: float) -> torch.Tensor:
        values = []
        for lane, length in zip(records, lengths):
            column_values = lane[field][:time_steps] + [fill] * max(0, time_steps - length)
            values.append(column_values[:time_steps])
        return torch.tensor(values, dtype=torch.float32).transpose(0, 1)

    valid_rows = [
        [True] * length + [False] * (time_steps - length) for lane, length in zip(records, lengths)
    ]
    # Engine-finalized terminals settle the return, so no bootstrap is needed.
    return {
        "observations": tuple(observations),
        "actions": tuple(actions),
        "episode_start": column("episode_start", 0.0).bool(),
        "valid_mask": torch.tensor(valid_rows, dtype=torch.bool).transpose(0, 1),
        "behavior_log_prob": column("log_prob", 0.0),
        "behavior_value": column("value", 0.0),
        "next_value": torch.zeros(time_steps, batch),
        "rewards": column("reward", 0.0),
        "terminated": column("terminated", 0.0).bool(),
        "truncated": column("truncated", 0.0).bool(),
        "forced_gate_mask": column("forced", 0.0).bool(),
    }


def trainer(args) -> int:
    import torch

    from native_runner.training.v4.checkpoint import save_actor_critic_checkpoint
    from native_runner.training.v4.distributed_ppo import distributed_ppo_update_v4
    from native_runner.training.v4.policy_session import load_policy_v4
    from native_runner.training.v4.ppo import PPOConfigV4, StoredRolloutV4

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    rollout_root = Path(args.output) / "rollout"
    policy_dir = Path(args.output) / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    trained_policies = (args.policy,) if args.policy else POLICY_NAMES
    models: dict[str, object] = {}
    optimizers: dict[str, object] = {}
    scalers: dict[str, object] = {}
    steps: dict[str, int] = {}
    matrix: dict[str, dict[str, list]] = {}
    for policy in trained_policies:
        path = policy_checkpoint_path(policy_dir, policy)
        source = str(path) if path.exists() else args.base_checkpoint
        policy_data = load_policy_v4(source, device=device)
        models[policy] = policy_data.model
        optimizers[policy] = torch.optim.AdamW(
            models[policy].parameters(), lr=args.learning_rate, weight_decay=1e-2
        )
        scalers[policy] = torch.amp.GradScaler(
            "cuda", enabled=device.type == "cuda" and args.autocast == "float16"
        )
        steps[policy] = 0

    consumed_root = Path(args.output) / "consumed"
    stop_file = Path(args.output) / "STOP"
    stats_suffix = f"-{args.policy}" if args.policy else ""
    stats_path = Path(args.output) / f"league-stats{stats_suffix}.json"
    last_report = 0.0
    try:
        while not stop_file.exists():
            time.sleep(args.trainer_cycle_seconds)
            for policy in trained_policies:
                directory = rollout_root / policy
                if not directory.exists():
                    continue
                fresh = sorted(directory.glob("m-*.pt"))
                if len(fresh) < args.min_lanes:
                    continue
                selected = fresh[: args.max_lanes]
                records = []
                readable_paths = []
                for path in selected:
                    try:
                        payload = torch.load(path, map_location="cpu", weights_only=False)
                    except (EOFError, RuntimeError, OSError):
                        # A worker may still be completing a file from an old
                        # non-atomic writer. Leave it for the next cycle.
                        continue
                    records.append(_normalize_record(payload))
                    readable_paths.append(path)
                    opponent = str(payload.get("opponent_policy"))
                    winner = payload.get("winner")
                    bucket = matrix.setdefault(policy, {}).setdefault(opponent, [0, 0, 0])  # wins, losses, draws
                    if winner is None:
                        bucket[2] += 1
                    elif int(winner) == int(payload.get("owner")):
                        bucket[0] += 1
                    else:
                        bucket[1] += 1
                if len(records) < args.min_lanes:
                    continue
                selected = readable_paths
                parts = assemble_rollout(records)
                initial = models[policy].initial_state(parts["valid_mask"].shape[1], device="cpu")
                rollout = StoredRolloutV4(
                    observations=parts["observations"],
                    actions=parts["actions"],
                    episode_start=parts["episode_start"],
                    valid_mask=parts["valid_mask"],
                    behavior_log_prob=parts["behavior_log_prob"],
                    behavior_value=parts["behavior_value"],
                    next_value=parts["next_value"],
                    rewards=parts["rewards"],
                    terminated=parts["terminated"],
                    truncated=parts["truncated"],
                    forced_gate_mask=parts["forced_gate_mask"],
                    initial_state=initial,
                    gate_temperature=float(models[policy].ppo_gate_temperature),
                    action_temperature=float(models[policy].ppo_action_temperature),
                    continue_temperature=float(models[policy].ppo_continue_temperature),
                    policy_state_id=f"league-{policy}-{steps[policy]}",
                )
                models[policy].train()
                autocast_dtype = {
                    "none": None,
                    "float16": torch.float16,
                    "bfloat16": torch.bfloat16,
                }[args.autocast]
                distributed_ppo_update_v4(
                    models[policy],
                    optimizers[policy],
                    rollout,
                    epochs=args.epochs,
                    lane_minibatch_size=args.lane_minibatch,
                    sequence_chunk_steps=args.sequence_chunk_steps,
                    config=PPOConfigV4(
                        gamma=args.gamma,
                        gae_lambda=args.gae_lambda,
                        entropy_coefficient=args.entropy_coefficient,
                    ),
                    autocast_dtype=autocast_dtype if device.type == "cuda" else None,
                    grad_scaler=scalers[policy] if args.autocast == "float16" else None,
                    validate=False,
                )
                steps[policy] += 1
                save_actor_critic_checkpoint(
                    policy_checkpoint_path(policy_dir, policy),
                    models[policy],
                    optimizer=optimizers[policy],
                    grad_scaler=scalers[policy],
                    update_step=steps[policy],
                    training_stage=f"league-1x-{policy}",
                    gamma_per_decision=args.gamma,
                )
                # Trim on-policy surplus so collection never outpaces the
                # compute-bound updates on disk.
                directory = rollout_root / policy
                if directory.exists():
                    surplus = sorted(directory.glob("m-*.pt"), key=lambda item: item.stat().st_mtime)
                    for stale in surplus[: max(0, len(surplus) - args.keep_pending)]:
                        stale.unlink(missing_ok=True)
                if args.keep_consumed:
                    consumed = consumed_root / policy
                    consumed.mkdir(parents=True, exist_ok=True)
                    for path in selected:
                        path.rename(consumed / path.name)
                else:
                    # On-policy rollouts have no reuse value; delete to keep
                    # steady-state disk bounded (~one episode of records).
                    for path in selected:
                        path.unlink(missing_ok=True)
                print(
                    json.dumps(
                        {
                            "event": "ppo_update",
                            "policy": policy,
                            "step": steps[policy],
                            "lanes": len(records),
                            "frames": int(rollout.valid_mask.sum()),
                        }
                    ),
                    flush=True,
                )
            if time.time() - last_report > 300:
                last_report = time.time()
                stats_path.write_text(
                    json.dumps({"steps": steps, "matrix": matrix, "wall": time.time()})
                )
                print(json.dumps({"event": "stats", "steps": steps}), flush=True)
    finally:
        stats_path.write_text(json.dumps({"steps": steps, "matrix": matrix, "wall": time.time()}))
    return 0


def master(args) -> int:
    validate_league_decks()
    output = Path(args.output)
    rollout_dir = output / "rollout"
    policy_dir = output / "policy"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    policy_dir.mkdir(parents=True, exist_ok=True)
    stop_file = output / "STOP"
    stop_file.unlink(missing_ok=True)
    engines = []
    for serial, base in INSTANCES:
        for engine_id in range(ENGINES_PER_INSTANCE):
            engines.append((len(engines), serial, base + engine_id, engine_id))
    if args.engines:
        selected = {int(value) for value in args.engines.split(",")}
        engines = [entry for entry in engines if entry[0] in selected]
    engines = [
        (engine_index + args.engine_index_offset, serial, port, engine_id)
        for engine_index, serial, port, engine_id in engines
    ]
    for serial in sorted({entry[1] for entry in engines}):
        host_base = next(base for candidate, base in INSTANCES if candidate == serial)
        ready = True
        for engine_id in range(ENGINES_PER_INSTANCE):
            try:
                attestation = control(host_base + engine_id, "attest", timeout=8.0).get("attestation", {})
                ready = ready and attestation.get("production_ready") is True
            except Exception:  # noqa: BLE001
                ready = False
        if not ready:
            print(f"[{time.strftime('%H:%M:%S')}] {serial} not ready -> full engine restart", flush=True)
            restart_instance_engines(serial, host_base, range(ENGINES_PER_INSTANCE))
    env_base = os.environ.copy()
    env_base.update(
        {
            "BASE_CHECKPOINT": args.base_checkpoint,
            "ROLLOUT_DIR": str(rollout_dir),
            "POLICY_DIR": str(policy_dir),
            "STOP_FILE": str(stop_file),
            "LANES": str(args.lanes),
            "SHAPING_BETA": str(args.shaping_beta),
            "OVERFLOW_BETA": str(args.overflow_beta),
            "REWARD_PLAY": str(args.reward_play),
            "TERMINAL_MARGIN_BETA": str(args.terminal_margin_beta),
            "EXPLORE_GATE_TEMPERATURE": str(args.explore_gate_temperature),
            "FORCE_ACT_PROBABILITY": str(args.force_act_probability),
            "CR_RELAX_RING_CURSORS": "1",
        }
    )
    procs: dict[int, subprocess.Popen] = {}
    for entry in engines:
        engine_index, serial, port, engine_id = entry
        env = dict(env_base)
        env.update(
            {
                "ENGINE_INDEX": str(engine_index),
                "HOST_PORT": str(port),
                "CUDA_VISIBLE_DEVICES": str(engine_index % args.gpus),
            }
        )
        procs[engine_index] = subprocess.Popen(
            [sys.executable, __file__, "--worker"],
            env=env,
            stdout=(output / f"worker-{engine_index:02d}.log").open("a", buffering=1),
            stderr=subprocess.STDOUT,
        )

    def spawn_worker(engine_index: int) -> subprocess.Popen:
        entry = next(item for item in engines if item[0] == engine_index)
        port = entry[2]
        env = dict(env_base)
        env.update(
            {
                "ENGINE_INDEX": str(engine_index),
                "HOST_PORT": str(port),
                "CUDA_VISIBLE_DEVICES": str(engine_index % args.gpus),
            }
        )
        return subprocess.Popen(
            [sys.executable, __file__, "--worker"],
            env=env,
            stdout=(output / f"worker-{engine_index:02d}.log").open("a", buffering=1),
            stderr=subprocess.STDOUT,
        )

    def spawn_trainer(policy: str | None = None, gpu_index: int = 0) -> subprocess.Popen:
        env = dict(env_base)
        env["CUDA_VISIBLE_DEVICES"] = str((args.trainer_gpu + gpu_index) % args.gpus)
        command = [
                sys.executable,
                __file__,
                "--trainer",
                "--base-checkpoint",
                args.base_checkpoint,
                "--output",
                str(output),
                "--min-lanes",
                str(args.min_lanes),
                "--epochs",
                str(args.epochs),
                "--learning-rate",
                str(args.learning_rate),
                "--gamma",
                str(args.gamma),
                "--trainer-cycle-seconds",
                str(args.trainer_cycle_seconds),
                "--max-lanes",
                str(args.max_lanes),
                "--autocast",
                args.autocast,
        ]
        if policy is not None:
            command.extend(["--policy", policy])
        return subprocess.Popen(
            command,
            env=env,
            stdout=(output / f"trainer-{policy or 'all'}.log").open("a", buffering=1),
            stderr=subprocess.STDOUT,
        )

    trainer_procs = (
        {}
        if args.worker_only
        else {policy: spawn_trainer(policy, index) for index, policy in enumerate(POLICY_NAMES)}
    )
    mode = "worker-only" if args.worker_only else f"{len(trainer_procs)} parallel trainers (gpu {args.trainer_gpu}+)"
    print(
        f"league started: {len(engines)} engines x {args.lanes} lanes, {mode}",
        flush=True,
    )
    deadline = time.time() + args.hours * 3600.0
    try:
        while time.time() < deadline:
            time.sleep(30)
            for engine_index in list(procs):
                if procs[engine_index].poll() is not None:
                    procs[engine_index] = spawn_worker(engine_index)
                    print(f"[{time.strftime('%H:%M:%S')}] respawned worker engine{engine_index:02d}", flush=True)
            for policy, trainer_proc in list(trainer_procs.items()):
                if trainer_proc.poll() is not None:
                    index = POLICY_NAMES.index(policy)
                    trainer_procs[policy] = spawn_trainer(policy, index)
                    print(f"[{time.strftime('%H:%M:%S')}] respawned trainer {policy}", flush=True)
    finally:
        stop_file.write_text("done")
        for proc in procs.values():
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                proc.kill()
        for trainer_proc in trainer_procs.values():
            try:
                trainer_proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                trainer_proc.kill()
    print("league finished", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--worker", action="store_true")
    modes.add_argument("--trainer", action="store_true")
    parser.add_argument("--base-checkpoint", default="checkpoints/General/checkpoint-step-00000460.pt")
    parser.add_argument("--output", default="runs/league")
    parser.add_argument("--engines", default="", help="comma-separated engine indices (default all)")
    parser.add_argument("--engine-index-offset", type=int, default=0)
    parser.add_argument("--worker-only", action="store_true")
    parser.add_argument("--lanes", type=int, default=6)
    parser.add_argument("--gpus", type=int, default=8)
    parser.add_argument("--trainer-gpu", type=int, default=0)
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--shaping-beta", type=float, default=0.0)
    parser.add_argument("--overflow-beta", type=float, default=0.0)
    parser.add_argument("--reward-play", type=float, default=0.0)
    # Weight on the terminal win margin (crowns + weighted tower damage). The
    # environment rejects values that would let a lopsided loss outscore a
    # narrow win, i.e. >= 0.5 / 8.
    parser.add_argument("--terminal-margin-beta", type=float, default=0.05)
    parser.add_argument("--explore-gate-temperature", type=float, default=1.0)
    parser.add_argument("--force-act-probability", type=float, default=0.0)
    parser.add_argument("--entropy-coefficient", type=float, default=0.05)
    parser.add_argument("--min-lanes", type=int, default=24)
    parser.add_argument("--max-lanes", type=int, default=48)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=3e-5)
    parser.add_argument("--gamma", type=float, default=0.999)
    parser.add_argument("--gae-lambda", type=float, default=0.95)
    parser.add_argument("--lane-minibatch", type=int, default=8)
    parser.add_argument("--sequence-chunk-steps", type=int, default=32)
    parser.add_argument("--trainer-cycle-seconds", type=int, default=30)
    parser.add_argument("--autocast", choices=("none", "float16", "bfloat16"), default="float16")
    parser.add_argument("--keep-consumed", action="store_true")
    parser.add_argument("--policy", default="", help="trainer mode: restrict to one policy")
    parser.add_argument("--keep-pending", type=int, default=64)
    args = parser.parse_args()
    if args.worker:
        return worker()
    if args.trainer:
        return trainer(args)
    return master(args)


if __name__ == "__main__":
    raise SystemExit(main())
