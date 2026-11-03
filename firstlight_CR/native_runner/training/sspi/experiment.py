"""Finite, reproducible post-episode SSPI pilot orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

import torch

from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi.collector import (
    DEFAULT_TRANSPORT,
    collect_episode,
    episode_seed_identity,
    resolve_belief_ports,
)
from native_runner.training.sspi.augmentation import TEMPORAL_AUX_WEIGHT, TEMPORAL_SCHEMA
from native_runner.training.sspi.learning import (
    EFFECTIVE_BATCH,
    decisive_result,
    create_optimizer,
    load_checkpoint,
    load_episode_training_rows,
    load_episode_validation_rows,
    save_checkpoint,
    search_root_loss,
    train_generation_step,
)
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.value_calibration import stage_config, run_direct_value_stage
from native_runner.training.v4.factory import production_semantic_bundle


MAX_RECOVERABLE_QUARANTINES_PER_GENERATION = 4


def _emit(stage, **fields):
    print(json.dumps(dict(stage=stage, **fields), sort_keys=True, allow_nan=False), flush=True)


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _experiment_source_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _append_direct_value_stage(*, config, run_dir, generation, model, optimizer,
        optimizer_steps, train_dirs, actual_port, belief_port):
    if stage_config(config) is None:
        return None
    engine_sha = json.loads((train_dirs[0] / "episode.json").read_text())["identity"].get("engine_sha256")
    return run_direct_value_stage(run_dir=run_dir, config=config, generation=generation,
        model=model, optimizer=optimizer, optimizer_steps=optimizer_steps,
        engine_sha=engine_sha, actual_port=actual_port, belief_port=belief_port,
        transport=_configured_transport(config),
        progress=lambda row: _write_status(run_dir, "running", row["stage"],
            **{key: value for key, value in row.items() if key not in ("status", "stage")}))


def exposure_batches(rows, *, seed, exposure_limit=2, batch_size=EFFECTIVE_BATCH):
    """Make full unique-root batches without exceeding each root's exposure cap."""
    if (type(seed) is not int or type(exposure_limit) is not int or exposure_limit < 1
            or type(batch_size) is not int or batch_size < 1):
        raise ValueError("invalid deterministic exposure schedule")
    if len(rows) < batch_size:
        return []
    tokens = [(index, exposure) for exposure in range(exposure_limit) for index in range(len(rows))]
    random.Random(seed).shuffle(tokens)
    batches = []
    while len(tokens) >= batch_size:
        selected, deferred, seen = [], [], set()
        for token in tokens:
            if len(selected) < batch_size and token[0] not in seen:
                selected.append(token)
                seen.add(token[0])
            else:
                deferred.append(token)
        if len(selected) != batch_size:
            break
        batches.append(selected)
        tokens = deferred
    return batches


def select_generation_batch(rows, *, seed, batch_size=EFFECTIVE_BATCH):
    """Select one deterministic, unique SEARCH-root batch for a generation."""
    if type(seed) is not int or type(batch_size) is not int or batch_size < 1:
        raise ValueError("invalid generation batch selection identity")
    if len(rows) < batch_size:
        raise ValueError(f"generation did not collect {batch_size} SEARCH roots")
    return random.Random(seed).sample(range(len(rows)), batch_size)


def paired_score_interval(pair_scores, *, seed, replicates=10_000):
    """Legacy equal-weight score helper; active WL reports use paired_win_rate_interval."""
    if not pair_scores or any(not 0. <= value <= 1. for value in pair_scores):
        raise ValueError("paired evaluation requires complete seed-pair scores")
    rng = random.Random(seed)
    count = len(pair_scores)
    samples = sorted(sum(pair_scores[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(replicates))
    low = samples[int(0.025 * (replicates - 1))]
    high = samples[int(0.975 * (replicates - 1))]
    return dict(mean=sum(pair_scores) / count, low=low, high=high, method="paired-seed-bootstrap",
        replicates=replicates, seed=seed)


def paired_win_rate_interval(pair_counts, *, seed, replicates=10_000):
    """Resample whole seed pairs; use wins / decisive games, not mean pair rates."""
    if (not pair_counts or type(replicates) is not int or replicates < 1
            or any(len(pair) != 2 or any(type(x) is not int for x in pair)
                or not 0 <= pair[0] <= pair[1] <= 2 for pair in pair_counts)):
        raise ValueError("paired WL evaluation requires valid per-pair win and decisive counts")
    wins, decisive = map(sum, zip(*pair_counts))
    result = dict(mean=None, low=None, high=None, wins=wins, losses=decisive-wins,
        decisive_games=decisive, excluded_draw_games=2*len(pair_counts)-decisive,
        complete_pairs=len(pair_counts), method="paired-seed-bootstrap-win-over-decisive",
        replicates=replicates, effective_replicates=0, seed=seed, status="undefined")
    if not decisive:
        return result
    rng, samples = random.Random(seed), []
    for _ in range(replicates):
        drawn = [pair_counts[rng.randrange(len(pair_counts))] for _ in pair_counts]
        total = sum(pair[1] for pair in drawn)
        if total:
            samples.append(sum(pair[0] for pair in drawn) / total)
    samples.sort()
    result.update(mean=wins/decisive, status="defined", effective_replicates=len(samples))
    if samples:
        result.update(low=samples[int(.025*(len(samples)-1))],
            high=samples[int(.975*(len(samples)-1))])
    return result


def episode_outcome_counts(directories):
    decisive = sum(decisive_result(json.loads((Path(path)/"episode.json").read_text())["result"])
        for path in directories)
    return dict(collected_games=len(directories), decisive_games=decisive,
        excluded_draw_games=len(directories)-decisive)


def _skip_generation(run_dir, generation, reason, train_dirs, validation_dirs):
    record = dict(status="skipped", generation=generation, reason=reason,
        optimizer_steps=0, training_outcomes=episode_outcome_counts(train_dirs),
        validation_outcomes=episode_outcome_counts(validation_dirs))
    _atomic_json(Path(run_dir)/f"generation-{generation:04d}-skipped.json", record)
    _write_status(run_dir, "skipped", "generation_update_skipped", **{k:v for k,v in record.items()
        if k != "status"})
    return record


def _pid_command(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
        check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def wait_for_episode_process(pid, episode_dir, *, expected_game_id, poll_seconds=15.):
    if type(pid) is not int or pid <= 1 or poll_seconds <= 0.:
        raise ValueError("invalid active episode process identity")
    episode_dir = Path(episode_dir)
    marker = f"--game-id {expected_game_id}"
    command = _pid_command(pid)
    if command and ("native_runner.training.sspi.collector" not in command or marker not in command):
        raise RuntimeError("the requested PID is not the expected SSPI collector")
    _emit("waiting_for_natural_terminal", pid=pid, game_id=expected_game_id,
        episode_dir=str(episode_dir), poll_seconds=poll_seconds)
    while command:
        time.sleep(poll_seconds)
        command = _pid_command(pid)
        if command and ("native_runner.training.sspi.collector" not in command or marker not in command):
            command = ""
    manifest = episode_dir / "episode.json"
    quarantine = episode_dir / "quarantine.json"
    deadline = time.monotonic() + 30.
    while not manifest.exists() and not quarantine.exists() and time.monotonic() < deadline:
        time.sleep(1.)
    if quarantine.exists():
        raise RuntimeError(f"active native episode was quarantined: {quarantine.read_text()}")
    if not manifest.exists():
        raise RuntimeError("collector exited without a complete episode or quarantine record")
    metadata = json.loads(manifest.read_text())
    if (metadata.get("status") != "complete" or
            metadata.get("training_admissible") is not decisive_result(metadata.get("result"))):
        raise RuntimeError("active collector did not publish a training-admissible natural terminal")
    return metadata


def _episode_model_sha(metadata):
    identity = metadata.get("identity", {})
    by_owner = identity.get("model_sha256_by_owner")
    if isinstance(by_owner, dict) and by_owner.get("0") == by_owner.get("1"):
        return by_owner["0"]
    return identity.get("model_sha256")


def _configured_transport(run_config):
    transport = run_config.get("transport", DEFAULT_TRANSPORT)
    if transport not in ("legacy", "persistent", "event-json", "event-json-delta"):
        raise ValueError("SSPI run config has an unknown native transport")
    return transport


def _validate_episode(metadata, *, run_config, model_sha, episode_kind, search_probability):
    identity = metadata.get("identity", {})
    decisive = decisive_result(metadata.get("result"))
    if (metadata.get("schema") != "sspi-mac-pilot-episode.v2"
            or metadata.get("status") != "complete"
            or metadata.get("episode_kind", "training") != episode_kind
            or metadata.get("training_admissible", True) is not (episode_kind == "training" and decisive)
            or metadata.get("search_probability") != search_probability
            or identity.get("run_id") != run_config["run_id"]
            or identity.get("arm") != run_config["arm"]
            or identity.get("seed") != run_config["seed"]
            or identity.get("device") != "cpu"
            or (episode_kind != "evaluation" and _episode_model_sha(metadata) != model_sha)):
        raise ValueError(f"episode identity differs from the expected {episode_kind} protocol")


def _write_status(run_dir, status, stage, **fields):
    if status == "running":
        fields.setdefault("owner_pid", os.getpid())
    _atomic_json(Path(run_dir) / "postgame_status.json",
        dict(status=status, stage=stage, **fields))


def _collect_one(*, run_config, run_dir, game_id, episode_kind, model, device,
        actual_port, belief_port, transport, random_stream_id=None):
    output = run_dir / "episodes" / game_id
    shared_model = None if isinstance(model, dict) else model
    owner_models = model if isinstance(model, dict) else None
    configured_belief_ports = run_config.get("ports", {}).get("belief_by_owner")
    if isinstance(configured_belief_ports, dict):
        configured_belief_ports = {int(owner): port
            for owner, port in configured_belief_ports.items()}
    belief_ports = resolve_belief_ports(actual_port, belief_port, configured_belief_ports)
    episode = collect_episode(actual_port=actual_port, belief_port=belief_port,
        actual_env_id=run_config.get("actual_env_id", 0),
        output_dir=output, run_id=run_config["run_id"], arm=run_config["arm"],
        game_id=game_id, seed=run_config["seed"], model=shared_model,
        transport=transport, episode_kind=episode_kind,
        search_probability=0. if episode_kind == "evaluation" else 0.05,
        temporal_augmentation=run_config.get("temporal_augmentation", True),
        random_stream_id=random_stream_id,
        models_by_owner=owner_models, device=device,
        belief_ports=belief_ports,
        progress=lambda row: print(json.dumps(row, sort_keys=True, allow_nan=False), flush=True))
    return output, episode


def _episode_prefix(kind, generation=None):
    if generation is None:
        return f"{kind}-game-"
    return f"{kind}-g{generation:04d}-game-"


def _is_recoverable_quarantine(directory):
    path = Path(directory) / "quarantine.json"
    if not path.is_file() or (Path(directory) / "episode.json").exists():
        return False
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (record.get("status") == "quarantined"
        and record.get("training_admissible") is False
        and record.get("exception") == "IncompatibleHistory"
        and record.get("truncated") is False and record.get("ended") is False)


def _combined_quarantine_ids(training_ids, validation_ids):
    if len(training_ids) > MAX_RECOVERABLE_QUARANTINES_PER_GENERATION:
        raise RuntimeError("generation exceeded the recoverable training quarantine limit")
    if len(validation_ids) > MAX_RECOVERABLE_QUARANTINES_PER_GENERATION:
        raise RuntimeError("generation exceeded the recoverable validation quarantine limit")
    return list(dict.fromkeys([*training_ids, *validation_ids]))


def _existing_episode_batch(run_dir, *, prefix):
    """Load complete episodes and preserve, but exclude, eligible quarantines."""
    episodes_root = Path(run_dir) / "episodes"
    complete, quarantined, max_index = [], [], 0
    if not episodes_root.exists():
        return complete, quarantined, 1
    for directory in sorted(episodes_root.iterdir()):
        if not directory.is_dir() or not directory.name.startswith(prefix):
            continue
        index_text = directory.name[len(prefix):]
        if not index_text.isdecimal():
            continue
        max_index = max(max_index, int(index_text))
        manifest = directory / "episode.json"
        quarantine = directory / "quarantine.json"
        if manifest.is_file() and quarantine.exists():
            raise RuntimeError(f"episode has both completion and quarantine records: {directory}")
        if manifest.is_file():
            complete.append((int(index_text), directory, json.loads(manifest.read_text())))
        elif _is_recoverable_quarantine(directory):
            quarantined.append(directory.name)
        else:
            raise RuntimeError(f"incomplete episode needs recovery: {directory}")
    if len(quarantined) > MAX_RECOVERABLE_QUARANTINES_PER_GENERATION:
        raise RuntimeError("generation exceeded the recoverable quarantine limit")
    return complete, quarantined, max_index + 1


def _collect_or_skip_quarantine(*, run_config, run_dir, game_id, episode_kind,
        model, device, actual_port, belief_port, transport, quarantined_game_ids):
    output = Path(run_dir) / "episodes" / game_id
    if output.exists():
        if _is_recoverable_quarantine(output):
            if game_id not in quarantined_game_ids:
                if len(quarantined_game_ids) >= MAX_RECOVERABLE_QUARANTINES_PER_GENERATION:
                    raise RuntimeError("generation exceeded the recoverable quarantine limit")
                quarantined_game_ids.append(game_id)
            _emit("episode_quarantined", game_id=game_id,
                reason=json.loads((output / "quarantine.json").read_text()).get("reason"),
                replacement_required=True)
            return None, None
        manifest = output / "episode.json"
        if manifest.is_file() and not (output / "quarantine.json").exists():
            return output, json.loads(manifest.read_text())
        raise RuntimeError(f"episode output already exists without a recoverable result: {output}")
    try:
        return _collect_one(run_config=run_config, run_dir=Path(run_dir),
            game_id=game_id, episode_kind=episode_kind, model=model, device=device,
            actual_port=actual_port, belief_port=belief_port, transport=transport)
    except Exception:
        if not _is_recoverable_quarantine(output):
            raise
        if len(quarantined_game_ids) >= MAX_RECOVERABLE_QUARANTINES_PER_GENERATION:
            raise
        quarantined_game_ids.append(game_id)
        _emit("episode_quarantined", game_id=game_id,
            reason=json.loads((output / "quarantine.json").read_text()).get("reason"),
            replacement_required=True)
        return None, None


def _generation_episode_id(kind, generation, index):
    if kind not in ("A", "V") or type(generation) is not int or generation < 2 \
            or type(index) is not int or index < 1:
        raise ValueError("invalid continuation episode identity")
    return f"{kind}-g{generation:04d}-game-{index:04d}"


def paired_outcomes_from_results(results, *, expected_pairs):
    if type(expected_pairs) is not int or expected_pairs < 1 or len(results) != expected_pairs * 2:
        raise ValueError("paired evaluation requires exactly two complete games per pair")
    grouped, game_ids = {}, set()
    for result in results:
        pair = result.get("pair_index")
        side = result.get("side")
        score = result.get("score")
        game_id = result.get("game_id")
        if (type(pair) is not int or not 1 <= pair <= expected_pairs
                or type(side) is not int or side not in (0, 1)
                or result.get("trained_owner") != side
                or type(game_id) is not str or not game_id or game_id in game_ids
                or "winner" not in result
                or (result["winner"] is not None and
                    (type(result["winner"]) is not int or result["winner"] not in (0, 1)))
                or (score is not None if result.get("winner") is None else
                    type(score) not in (int, float) or score != float(result["winner"] == side))
                or type(result.get("search_roots")) is not int
                or result.get("search_roots") != 0
                or result.get("search_probability") != 0.):
            raise ValueError("paired evaluation result has an invalid side, score, or search count")
        game_ids.add(game_id)
        grouped.setdefault(pair, []).append(result)

    pair_scores, stream_ids, native_seed_pairs = [], set(), set()
    for pair in range(1, expected_pairs + 1):
        games = grouped.get(pair, [])
        if len(games) != 2 or {game.get("side") for game in games} != {0, 1}:
            raise ValueError("paired evaluation is missing a side-swapped game")
        first, second = sorted(games, key=lambda game: game["side"])
        if ({first.get("trained_owner"), second.get("trained_owner")} != {0, 1}
                or type(first.get("random_stream_id")) is not str
                or not first.get("random_stream_id")
                or first.get("random_stream_id") != second.get("random_stream_id")
                or type(first.get("actual_match_seed")) is not int
                or type(first.get("actual_rollout_seed")) is not int
                or first.get("actual_match_seed") != second.get("actual_match_seed")
                or first.get("actual_rollout_seed") != second.get("actual_rollout_seed")):
            raise ValueError("side-swapped games do not share the same recorded random streams")
        if first["random_stream_id"] in stream_ids:
            raise ValueError("independent evaluation pairs reused a random stream identity")
        stream_ids.add(first["random_stream_id"])
        native_seeds = (first["actual_match_seed"], first["actual_rollout_seed"])
        if native_seeds in native_seed_pairs:
            raise ValueError("independent evaluation pairs reused an actual native seed pair")
        native_seed_pairs.add(native_seeds)
        scores = [game["score"] for game in (first, second) if game["score"] is not None]
        pair_scores.append((int(sum(scores)), len(scores)))
    return pair_scores


def _run_paired_evaluation(*, run_config, run_dir, generation, trained_model,
        trained_model_sha, theta0_model, theta0_sha, checkpoint_sha, actual_port,
        belief_port, transport, evaluation_pairs, output_name):
    if type(evaluation_pairs) is not int or evaluation_pairs < 1:
        raise ValueError("paired evaluation requires at least one independent seed pair")
    run_dir = Path(run_dir)
    output_path = run_dir / output_name
    if output_path.exists():
        raise ValueError(f"paired evaluation output already exists: {output_path}")

    results = []
    for pair_index in range(evaluation_pairs):
        pair_number = pair_index + 1
        stream_id = f"sspi-eval-g{generation:04d}-pair-{pair_number:04d}"
        for side in (0, 1):
            game_number = pair_index * 2 + side + 1
            game_id = f"E-paired-g{generation:04d}-game-{game_number:04d}"
            trained_owner = side
            models = {trained_owner: trained_model, 1 - trained_owner: theta0_model}
            expected_models = {str(trained_owner): trained_model_sha,
                str(1 - trained_owner): theta0_sha}
            directory = run_dir / "episodes" / game_id
            if directory.exists():
                manifest = directory / "episode.json"
                quarantine = directory / "quarantine.json"
                if quarantine.exists() or not manifest.is_file():
                    raise RuntimeError(f"paired evaluation episode needs inspection: {directory}")
                metadata = json.loads(manifest.read_text())
            else:
                directory, metadata = _collect_one(run_config=run_config, run_dir=run_dir,
                    game_id=game_id, episode_kind="evaluation", model=models, device="cpu",
                    actual_port=actual_port, belief_port=belief_port, transport=transport,
                    random_stream_id=stream_id)
            _validate_episode(metadata, run_config=run_config, model_sha=theta0_sha,
                episode_kind="evaluation", search_probability=0.)
            identity = metadata["identity"]
            if (identity.get("random_stream_id") != stream_id
                    or identity.get("game_id") != game_id
                    or identity.get("model_sha256_by_owner") != expected_models
                    or type(identity.get("actual_match_seed")) is not int
                    or type(identity.get("actual_rollout_seed")) is not int
                    or type(metadata.get("search_roots")) is not int
                    or metadata.get("search_roots") != 0):
                raise ValueError("paired evaluation episode identity differs from its planned side/stream")
            expected_seed_identity = episode_seed_identity(run_config["seed"],
                run_id=run_config["run_id"], arm=run_config["arm"], game_id=game_id,
                random_stream_id=stream_id)
            if any(identity.get(key) != value for key, value in expected_seed_identity.items()):
                raise ValueError("paired evaluation episode recorded unexpected random stream seeds")
            winner = metadata["result"]["winner"]
            if winner is not None and (type(winner) is not int or winner not in (0, 1)):
                raise ValueError("paired evaluation episode has an invalid terminal winner")
            score = None if winner is None else 1. if winner == trained_owner else 0.
            result = dict(pair_index=pair_number, side=side, game_id=game_id,
                trained_owner=trained_owner, winner=winner, score=score,
                random_stream_id=identity["random_stream_id"],
                actual_match_seed=identity["actual_match_seed"],
                actual_rollout_seed=identity["actual_rollout_seed"],
                search_roots=metadata["search_roots"],
                search_probability=metadata["search_probability"],
                episode=str(directory.relative_to(run_dir)),
                episode_manifest_sha256=hashlib.sha256((directory / "episode.json").read_bytes()).hexdigest())
            results.append(result)
            _emit("paired_evaluation_game_complete", generation=generation,
                pair=pair_number, side=side, games=len(results),
                total_games=evaluation_pairs * 2, winner=winner, score=score)
            _write_status(run_dir, "running", "paired_evaluation", generation=generation,
                games=len(results), target_games=evaluation_pairs * 2,
                completed_pairs=len({item["pair_index"] for item in results}))

    pair_counts = paired_outcomes_from_results(results, expected_pairs=evaluation_pairs)
    score = paired_win_rate_interval(pair_counts,
        seed=run_config["seed"] ^ generation ^ 0x53535049)
    record = dict(schema="sspi-mac-pilot-paired-evaluation.v2",
        run_id=run_config["run_id"], generation=generation,
        trained_checkpoint_sha256=checkpoint_sha, theta0_sha256=theta0_sha,
        protocol=dict(search_probability=0., temperature=1., paired_seeds=evaluation_pairs,
            games=len(results), games_per_seed=2, pairing_key="random_stream_id",
            deck=list(run_config["deck"])), pair_counts=pair_counts, score=score,
        results=results)
    _atomic_json(output_path, record)
    return record


def evaluate_generation(*, run_dir, generation, actual_port=26789, belief_port=26790,
        evaluation_pairs=32, transport=None):
    """Restore a completed generation and run its resumable no-search seed-pair evaluation."""
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "run_config.json").read_text())
    if (type(generation) is not int or generation < 1
            or config.get("schema") != "sspi-mac-pilot-run.v2"
            or config.get("arm") != "A_5PCT_128" or config.get("device") != "cpu"
            or type(evaluation_pairs) is not int or evaluation_pairs < 1):
        raise ValueError("generation evaluation identity or execution plan differs from the Mac run")
    status_path = run_dir / "postgame_status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    if status.get("status") == "running":
        owner_pid = status.get("owner_pid")
        if type(owner_pid) is not int:
            raise RuntimeError("cannot verify the owner of the active SSPI orchestrator")
        if _pid_command(owner_pid):
            raise RuntimeError("an SSPI postgame or continuation orchestrator is still running")
    configured_transport = _configured_transport(config)
    if transport is not None and transport != configured_transport:
        raise ValueError("generation evaluation transport differs from the frozen run configuration")
    bundle = production_semantic_bundle()
    trained_model, _optimizer, generation_record, restored = _restore_generation_checkpoint(
        run_dir, generation, config["run_id"], bundle=bundle)
    if (generation_record.get("direct_value_stage") or {}).get("plan") != stage_config(config):
        raise ValueError("generation direct value plan differs from frozen run configuration")
    baseline = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle).eval()
    baseline_sha = semantic_digest(baseline.state_dict())
    initialization = config.get("initialization", {})
    initialization_path = Path(initialization.get("path", ""))
    if (not initialization_path.is_file()
            or hashlib.sha256(initialization_path.read_bytes()).hexdigest() != initialization.get("file_sha")
            or baseline_sha != initialization.get("model_sha")
            or baseline_sha != generation_record.get("theta0_sha256")):
        raise ValueError("restored theta0 differs from the run initialization identity")
    for model in (trained_model, baseline):
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(False)
    output_name = f"evaluation-paired-generation-{generation:04d}.json"
    return _run_paired_evaluation(run_config=config, run_dir=run_dir, generation=generation,
        trained_model=trained_model, trained_model_sha=restored["model_sha"],
        theta0_model=baseline, theta0_sha=baseline_sha,
        checkpoint_sha=restored["file_sha"], actual_port=actual_port,
        belief_port=belief_port, transport=configured_transport,
        evaluation_pairs=evaluation_pairs, output_name=output_name)


def _restore_generation_checkpoint(run_dir, generation, run_id, *, bundle):
    record_path = Path(run_dir) / f"generation-{generation:04d}.json"
    record = json.loads(record_path.read_text())
    saved = record.get("checkpoint") or {}
    checkpoint_path = Path(run_dir) / "checkpoints" / f"generation-{generation:04d}.pt"
    if (record.get("schema") != "sspi-mac-pilot-generation.v2"
            or record.get("run_id") != run_id or record.get("generation") != generation
            or saved.get("path") != str(checkpoint_path)
            or not checkpoint_path.is_file()
            or hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() != saved.get("file_sha")):
        raise ValueError("parent generation record or checkpoint identity differs")

    envelope = torch.load(checkpoint_path, weights_only=True, map_location="cpu")
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    identity = payload.get("identity") if isinstance(payload, dict) else None
    if (not isinstance(identity, dict) or identity.get("run_id") != run_id
            or identity.get("generation") != generation
            or identity.get("direct_value_stage") != record.get("direct_value_stage")):
        raise ValueError("parent checkpoint does not identify the requested generation")
    model = initialize_fresh_model(identity["seed"], device="cpu", bundle=bundle)
    optimizer = create_optimizer(model)
    restored = load_checkpoint(checkpoint_path, model, optimizer,
        expected_identity=identity, device="cpu")
    if (restored["file_sha"] != saved["file_sha"]
            or restored["model_sha"] != saved.get("model_sha")
            or not optimizer.state):
        raise ValueError("parent checkpoint failed model and optimizer restoration")
    return model, optimizer, record, restored


def run_next_generation(*, run_dir, parent_generation, actual_port=26789,
        belief_port=26790, train_games=16, validation_games=4, evaluation_pairs=32):
    """Collect one generation, apply one root-batch update, restore, and optionally evaluate."""
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "run_config.json").read_text())
    value_plan = stage_config(config)
    status_path = run_dir / "postgame_status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    if status.get("status") == "running":
        owner_pid = status.get("owner_pid")
        if type(owner_pid) is not int:
            raise RuntimeError("cannot verify the owner of the active SSPI orchestrator")
        command = _pid_command(owner_pid)
        if command and "native_runner.training.sspi.experiment" in command \
                and str(run_dir) in command:
            raise RuntimeError("an SSPI postgame or continuation orchestrator is still running")
    if (type(parent_generation) is not int or parent_generation < 1
            or config.get("schema") != "sspi-mac-pilot-run.v2"
            or config.get("arm") != "A_5PCT_128" or config.get("device") != "cpu"
            or status.get("status") not in ("complete", "failed", "running")
            or (status.get("status") == "complete"
                and status.get("generation") != parent_generation)
            or (status.get("status") == "failed"
                and status.get("generation") not in (None, parent_generation))
            or train_games < 1 or validation_games < 1
            or (evaluation_pairs is not None
                and (type(evaluation_pairs) is not int or evaluation_pairs < 1))):
        raise ValueError("continuation identity or parent completion state differs")
    generation = parent_generation + 1
    generation_record = run_dir / f"generation-{generation:04d}.json"
    checkpoint_path = run_dir / "checkpoints" / f"generation-{generation:04d}.pt"
    if generation_record.exists():
        raise ValueError("next generation is already complete; continue from its checkpoint")

    bundle = production_semantic_bundle()
    model, optimizer, parent_record, parent_restore = _restore_generation_checkpoint(
        run_dir, parent_generation, config["run_id"], bundle=bundle)
    if (parent_record.get("direct_value_stage") or {}).get("plan") != value_plan:
        raise ValueError("direct value stage cannot be added to or removed from a historical run")
    parent_checkpoint_sha = parent_restore["file_sha"]
    parent_model_sha = parent_restore["model_sha"]
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model.eval()

    # Preserve the active run's collection protocol when its manifest predates this field.
    collection_config = dict(config)
    collection_config["temporal_augmentation"] = bool(config.get("temporal_augmentation", False))
    train_dirs, train_rows = [], []
    train_prefix = _episode_prefix("A", generation)
    existing_train, training_quarantines, index = _existing_episode_batch(
        run_dir, prefix=train_prefix)
    for _episode_index, directory, metadata in existing_train:
        _validate_episode(metadata, run_config=config, model_sha=parent_model_sha,
            episode_kind="training", search_probability=0.05)
        train_dirs.append(directory)
        train_rows.extend(load_episode_training_rows(directory))
    _write_status(run_dir, "running", "continuation_training_collection",
        generation=generation, parent_generation=parent_generation, games=len(train_dirs),
        search_roots=len(train_rows), target_games=train_games,
        quarantined_games=list(training_quarantines))
    while len(train_dirs) < train_games:
        game_id = _generation_episode_id("A", generation, index)
        directory, metadata = _collect_or_skip_quarantine(run_config=collection_config,
            run_dir=run_dir, game_id=game_id, episode_kind="training", model=model,
            device="cpu", actual_port=actual_port, belief_port=belief_port,
            transport=_configured_transport(config), quarantined_game_ids=training_quarantines)
        index += 1
        if directory is None:
            _write_status(run_dir, "running", "continuation_training_collection",
                generation=generation, parent_generation=parent_generation,
                games=len(train_dirs), search_roots=len(train_rows), target_games=train_games,
                quarantined_games=list(training_quarantines))
            continue
        _validate_episode(metadata, run_config=config, model_sha=parent_model_sha,
            episode_kind="training", search_probability=0.05)
        train_dirs.append(directory)
        train_rows.extend(load_episode_training_rows(directory))
        _write_status(run_dir, "running", "continuation_training_collection",
            generation=generation, parent_generation=parent_generation, games=len(train_dirs),
            search_roots=len(train_rows), target_games=train_games,
            quarantined_games=list(training_quarantines))


    validation_dirs, validation_rows = [], []
    validation_prefix = _episode_prefix("V", generation)
    existing_validation, validation_quarantines, validation_index = _existing_episode_batch(
        run_dir, prefix=validation_prefix)
    _combined_quarantine_ids(training_quarantines, validation_quarantines)
    for _episode_index, directory, metadata in existing_validation:
        _validate_episode(metadata, run_config=config, model_sha=parent_model_sha,
            episode_kind="validation", search_probability=0.05)
        validation_dirs.append(directory)
        validation_rows.extend(load_episode_validation_rows(directory))
    _write_status(run_dir, "running", "continuation_validation_collection",
        generation=generation, games=len(validation_dirs), target_games=validation_games,
        quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))
    index = validation_index
    while len(validation_dirs) < validation_games:
        game_id = _generation_episode_id("V", generation, index)
        directory, metadata = _collect_or_skip_quarantine(run_config=collection_config,
            run_dir=run_dir, game_id=game_id, episode_kind="validation", model=model,
            device="cpu", actual_port=actual_port, belief_port=belief_port,
            transport=_configured_transport(config), quarantined_game_ids=validation_quarantines)
        index += 1
        if directory is None:
            _write_status(run_dir, "running", "continuation_validation_collection",
                generation=generation, games=len(validation_dirs),
                target_games=validation_games,
                quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))
            continue
        _validate_episode(metadata, run_config=config, model_sha=parent_model_sha,
            episode_kind="validation", search_probability=0.05)
        validation_dirs.append(directory)
        validation_rows.extend(load_episode_validation_rows(directory))
        _write_status(run_dir, "running", "continuation_validation_collection",
            generation=generation, games=len(validation_dirs), target_games=validation_games,
            quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))

    if len(train_rows) < EFFECTIVE_BATCH or not validation_rows:
        return _skip_generation(run_dir, generation,
            "insufficient_effective_search_or_validation_samples", train_dirs, validation_dirs)

    selection_seed = config["seed"] ^ generation ^ 0x53535049
    selected_indices = select_generation_batch(train_rows, seed=selection_seed,
        batch_size=EFFECTIVE_BATCH)
    for parameter in model.parameters():
        parameter.requires_grad_(True)
    _write_status(run_dir, "running", "continuation_training", generation=generation,
        search_roots=len(train_rows), trained_search_roots=len(selected_indices), updates=1)
    exposure_counts = [0] * len(train_rows)
    for index in selected_indices:
        exposure_counts[index] = 1
    update = train_generation_step(model, optimizer,
        [train_rows[index] for index in selected_indices], validation_rows=validation_rows)
    updates = [update]
    _emit("continuation_optimizer_update", generation=generation,
        step=1, updates_available=1, **update)

    model.eval()
    with torch.no_grad():
        validation = search_root_loss(model, validation_rows)
    validation_metrics = {key: float(value.detach().cpu()) if isinstance(value, torch.Tensor) else value
        for key, value in validation.items()}
    train_episode_shas = {path.name: hashlib.sha256((path / "episode.json").read_bytes()).hexdigest()
        for path in train_dirs}
    validation_episode_shas = {path.name: hashlib.sha256((path / "episode.json").read_bytes()).hexdigest()
        for path in validation_dirs}
    source_shas = sorted({json.loads((path / "episode.json").read_text())
        ["identity"]["source_sha256"] for path in train_dirs + validation_dirs})
    initialization_model_sha = parent_record.get("theta0_sha256")
    if not initialization_model_sha:
        raise ValueError("parent generation omitted the fresh initialization identity")
    direct_value_stage = _append_direct_value_stage(config=config, run_dir=run_dir,
        generation=generation, model=model, optimizer=optimizer,
        optimizer_steps=parent_restore["optimizer_steps"] + len(updates), train_dirs=train_dirs,
        actual_port=actual_port, belief_port=belief_port)
    optimizer_steps = (parent_restore["optimizer_steps"] + len(updates)
        + (direct_value_stage or {}).get("optimizer_steps", 0))
    checkpoint_identity = dict(run_id=config["run_id"], generation=generation,
        seed=config["seed"], architecture_sha=model.architecture_sha,
        initialization_model_sha=initialization_model_sha,
        parent_checkpoint_sha256=parent_checkpoint_sha, parent_model_sha256=parent_model_sha,
        train_episode_sha256=train_episode_shas,
        validation_episode_sha256=validation_episode_shas, source_sha256=source_shas,
        experiment_source_sha256=_experiment_source_sha(),
        action_schema="sspi-tagged-action.v1",
        training_protocol=("5pct-x-128-guarded-plus-direct-value.v1" if direct_value_stage
            else "5pct-x-128-guarded-update-per-generation.v2"),
        training_root_selection_seed=selection_seed,
        training_root_indices=selected_indices,
        temporal_augmentation=collection_config["temporal_augmentation"],
        **(dict(direct_value_stage=direct_value_stage) if direct_value_stage else {}))
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer, identity=checkpoint_identity,
        optimizer_steps=optimizer_steps,
        run_state=dict(generation=generation, parent_checkpoint_sha256=parent_checkpoint_sha,
            training_games=[path.name for path in train_dirs],
            validation_games=[path.name for path in validation_dirs],
            search_roots=len(train_rows), trained_search_roots=len(selected_indices),
            training_root_selection_seed=selection_seed, training_root_indices=selected_indices,
            exposure_limit=2,
            exposure_counts=exposure_counts))
    restored = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle)
    restored_optimizer = create_optimizer(restored)
    restoration = load_checkpoint(checkpoint_path, restored, restored_optimizer,
        expected_identity=checkpoint_identity, device="cpu")
    if (semantic_digest(restored.state_dict()) != checkpoint["model_sha"]
            or restoration["optimizer_steps"] != optimizer_steps
            or not restored_optimizer.state):
        raise ValueError("continuation checkpoint did not restore model and optimizer state")

    record = dict(schema="sspi-mac-pilot-generation.v2", run_id=config["run_id"],
        generation=generation, parent_generation=parent_generation,
        parent_checkpoint_sha256=parent_checkpoint_sha, parent_model_sha256=parent_model_sha,
        theta0_sha256=initialization_model_sha, checkpoint=checkpoint,
        checkpoint_restore=restoration, training_games=[path.name for path in train_dirs],
        validation_games=[path.name for path in validation_dirs], search_roots=len(train_rows),
        trained_search_roots=len(selected_indices), unused_search_roots=len(train_rows) - len(selected_indices),
        training_root_selection_seed=selection_seed, training_root_indices=selected_indices,
        validation_roots=len(validation_rows), updates=updates, validation_loss=validation_metrics,
        training_outcomes=episode_outcome_counts(train_dirs),
        validation_outcomes=episode_outcome_counts(validation_dirs),
        exposure_limit=2, max_observed_exposure=max(exposure_counts),
        experiment_source_sha256=_experiment_source_sha(),
        quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines),
        temporal_augmentation=collection_config["temporal_augmentation"],
        **(dict(direct_value_stage=direct_value_stage) if direct_value_stage else {}))
    _atomic_json(generation_record, record)
    baseline = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle).eval()
    baseline_sha = semantic_digest(baseline.state_dict())
    if baseline_sha != initialization_model_sha:
        raise ValueError("continuation theta0 differs from the original initialization")
    for parameter in baseline.parameters():
        parameter.requires_grad_(False)
    if evaluation_pairs is None:
        record["evaluation"] = None
        _atomic_json(generation_record, record)
        _write_status(run_dir, "complete", "generation_complete", generation=generation,
            training_record=generation_record.name, evaluation_record=None,
            parent_generation=parent_generation)
        _emit("continuation_generation_complete", generation=generation,
            checkpoint=str(checkpoint_path), model_sha=checkpoint["model_sha"],
            optimizer_steps=restoration["optimizer_steps"], search_roots=len(train_rows),
            evaluation_deferred=True)
        return record

    evaluation = _run_paired_evaluation(run_config=config, run_dir=run_dir,
        generation=generation, trained_model=restored, trained_model_sha=checkpoint["model_sha"],
        theta0_model=baseline, theta0_sha=baseline_sha,
        checkpoint_sha=checkpoint["file_sha"], actual_port=actual_port,
        belief_port=belief_port, transport=_configured_transport(config),
        evaluation_pairs=evaluation_pairs,
        output_name=f"evaluation-paired-generation-{generation:04d}.json")
    record["evaluation"] = dict(path=f"evaluation-paired-generation-{generation:04d}.json",
        score=evaluation["score"], games=evaluation["protocol"]["games"])
    _atomic_json(generation_record, record)
    _write_status(run_dir, "complete", "generation_complete", generation=generation,
        training_record=generation_record.name, evaluation_record=record["evaluation"]["path"],
        parent_generation=parent_generation)
    _emit("continuation_generation_complete", generation=generation,
        checkpoint=str(checkpoint_path), model_sha=checkpoint["model_sha"],
        optimizer_steps=restoration["optimizer_steps"], search_roots=len(train_rows))
    return record


def run_postgame(*, run_dir, first_episode_dir, wait_pid, actual_port=26789,
        belief_port=26790, train_games=16, validation_games=4, evaluation_pairs=32,
        poll_seconds=15.):
    run_dir = Path(run_dir)
    first_episode_dir = Path(first_episode_dir)
    config = json.loads((run_dir / "run_config.json").read_text())
    value_plan = stage_config(config)
    previous_record = run_dir / "generation-0001.json"
    if previous_record.exists() and (json.loads(previous_record.read_text()).get(
            "direct_value_stage") or {}).get("plan") != value_plan:
        raise ValueError("direct value stage cannot be added to or removed from a historical run")
    if (config.get("schema") != "sspi-mac-pilot-run.v2" or config.get("status") != "collecting"
            or config.get("arm") != "A_5PCT_128" or config.get("device") != "cpu"
            or train_games < 1 or validation_games < 1
            or (evaluation_pairs is not None
                and (type(evaluation_pairs) is not int or evaluation_pairs < 1))):
        raise ValueError("postgame pilot identity or execution plan differs from the Mac run")

    first_metadata = wait_for_episode_process(wait_pid, first_episode_dir,
        expected_game_id="A-game-0001", poll_seconds=poll_seconds)
    _write_status(run_dir, "running", "initialize", completed_game="A-game-0001")
    bundle = production_semantic_bundle()
    baseline = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle).eval()
    baseline_sha = semantic_digest(baseline.state_dict())
    initialization_path = Path(config["initialization"]["path"])
    if (hashlib.sha256(initialization_path.read_bytes()).hexdigest() != config["initialization"]["file_sha"]
            or baseline_sha != config["initialization"]["model_sha"]
            or baseline_sha != _episode_model_sha(first_metadata)):
        raise ValueError("fresh theta0, saved initialization, and completed game identities differ")
    _validate_episode(first_metadata, run_config=config, model_sha=baseline_sha,
        episode_kind="training", search_probability=0.05)
    for parameter in baseline.parameters():
        parameter.requires_grad_(False)

    train_dirs, train_rows = [], []
    train_prefix = _episode_prefix("A")
    existing_train, training_quarantines, index = _existing_episode_batch(
        run_dir, prefix=train_prefix)
    for _episode_index, directory, metadata in existing_train:
        _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
            episode_kind="training", search_probability=0.05)
        train_dirs.append(directory)
        train_rows.extend(load_episode_training_rows(directory))
    if first_episode_dir not in train_dirs:
        raise ValueError("the original natural-terminal training episode is missing from the run")
    _write_status(run_dir, "running", "training_collection", games=len(train_dirs),
        search_roots=len(train_rows), target_games=train_games,
        quarantined_games=list(training_quarantines))
    while len(train_dirs) < train_games:
        game_id = f"A-game-{index:04d}"
        directory, metadata = _collect_or_skip_quarantine(run_config=config,
            run_dir=run_dir, game_id=game_id, episode_kind="training", model=baseline,
            device="cpu", actual_port=actual_port, belief_port=belief_port,
            transport=_configured_transport(config), quarantined_game_ids=training_quarantines)
        index += 1
        if directory is None:
            _write_status(run_dir, "running", "training_collection", games=len(train_dirs),
                search_roots=len(train_rows), target_games=train_games,
                quarantined_games=list(training_quarantines))
            continue
        if _episode_model_sha(metadata) != baseline_sha:
            raise ValueError("frozen training generation changed theta0 between episodes")
        _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
            episode_kind="training", search_probability=0.05)
        train_dirs.append(directory)
        train_rows.extend(load_episode_training_rows(directory))
        _emit("training_collection_progress", games=len(train_dirs), search_roots=len(train_rows),
            required_games=train_games, effective_batch=EFFECTIVE_BATCH,
            quarantined_games=list(training_quarantines))
        _write_status(run_dir, "running", "training_collection", games=len(train_dirs),
            search_roots=len(train_rows), target_games=train_games,
            quarantined_games=list(training_quarantines))


    validation_dirs, validation_rows = [], []
    validation_prefix = _episode_prefix("V")
    existing_validation, validation_quarantines, validation_index = _existing_episode_batch(
        run_dir, prefix=validation_prefix)
    _combined_quarantine_ids(training_quarantines, validation_quarantines)
    for _episode_index, directory, metadata in existing_validation:
        _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
            episode_kind="validation", search_probability=0.05)
        validation_dirs.append(directory)
        validation_rows.extend(load_episode_validation_rows(directory))
    _write_status(run_dir, "running", "validation_collection", games=len(validation_dirs),
        target_games=validation_games,
        quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))
    index = validation_index
    while len(validation_dirs) < validation_games:
        game_id = f"V-game-{index:04d}"
        directory, metadata = _collect_or_skip_quarantine(run_config=config,
            run_dir=run_dir, game_id=game_id, episode_kind="validation", model=baseline,
            device="cpu", actual_port=actual_port, belief_port=belief_port,
            transport=_configured_transport(config), quarantined_game_ids=validation_quarantines)
        index += 1
        if directory is None:
            _write_status(run_dir, "running", "validation_collection", games=len(validation_dirs),
                target_games=validation_games,
                quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))
            continue
        _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
            episode_kind="validation", search_probability=0.05)
        validation_dirs.append(directory)
        validation_rows.extend(load_episode_validation_rows(directory))
        _emit("validation_collection_progress", games=len(validation_dirs),
            search_roots=len(validation_rows), required_games=validation_games,
            quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))
        _write_status(run_dir, "running", "validation_collection", games=len(validation_dirs),
            search_roots=len(validation_rows), target_games=validation_games,
            quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines))

    if len(train_rows) < EFFECTIVE_BATCH or not validation_rows:
        return _skip_generation(run_dir, 1,
            "insufficient_effective_search_or_validation_samples", train_dirs, validation_dirs)

    selection_seed = config["seed"] ^ 1 ^ 0x53535049
    selected_indices = select_generation_batch(train_rows, seed=selection_seed,
        batch_size=EFFECTIVE_BATCH)
    _write_status(run_dir, "running", "training", games=len(train_dirs),
        search_roots=len(train_rows), trained_search_roots=len(selected_indices), updates=1)
    model = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle)
    optimizer = create_optimizer(model)
    exposure_counts = [0] * len(train_rows)
    for index in selected_indices:
        exposure_counts[index] = 1
    update = train_generation_step(model, optimizer,
        [train_rows[index] for index in selected_indices], validation_rows=validation_rows)
    updates = [update]
    _emit("optimizer_update", step=1, updates_available=1, **update)

    model.eval()
    with torch.no_grad():
        validation = search_root_loss(model, validation_rows)
    validation_metrics = {key: float(value.detach().cpu()) if isinstance(value, torch.Tensor) else value
        for key, value in validation.items()}
    train_episode_shas = {path.name: hashlib.sha256((path / "episode.json").read_bytes()).hexdigest()
        for path in train_dirs}
    validation_episode_shas = {path.name: hashlib.sha256((path / "episode.json").read_bytes()).hexdigest()
        for path in validation_dirs}
    direct_value_stage = _append_direct_value_stage(config=config, run_dir=run_dir,
        generation=1, model=model, optimizer=optimizer, optimizer_steps=len(updates),
        train_dirs=train_dirs, actual_port=actual_port, belief_port=belief_port)
    optimizer_steps = len(updates) + (direct_value_stage or {}).get("optimizer_steps", 0)
    checkpoint_identity = dict(run_id=config["run_id"], generation=1, seed=config["seed"],
        architecture_sha=model.architecture_sha, initialization_model_sha=baseline_sha,
        train_episode_sha256=train_episode_shas, validation_episode_sha256=validation_episode_shas,
        source_sha256=sorted({json.loads((path / "episode.json").read_text())["identity"]["source_sha256"]
            for path in train_dirs + validation_dirs}),
        action_schema="sspi-tagged-action.v1",
        training_protocol=("5pct-x-128-guarded-plus-direct-value.v1" if direct_value_stage
            else "5pct-x-128-guarded-update-per-generation.v2"),
        experiment_source_sha256=_experiment_source_sha(),
        training_root_selection_seed=selection_seed,
        training_root_indices=selected_indices,
        **(dict(direct_value_stage=direct_value_stage) if direct_value_stage else {}),
        continuation_protocol=dict(schema=TEMPORAL_SCHEMA,
            source_weight_with_samples=1. - TEMPORAL_AUX_WEIGHT,
            auxiliary_group_weight=TEMPORAL_AUX_WEIGHT,
            training_rows=sum(len(row.continuations) for row in train_rows),
            validation_rows=sum(len(row.continuations) for row in validation_rows)))
    checkpoint_path = run_dir / "checkpoints" / "generation-0001.pt"
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer, identity=checkpoint_identity,
        optimizer_steps=optimizer_steps, run_state=dict(training_games=[path.name for path in train_dirs],
            validation_games=[path.name for path in validation_dirs], search_roots=len(train_rows),
            trained_search_roots=len(selected_indices), training_root_selection_seed=selection_seed,
            training_root_indices=selected_indices,
            exposure_limit=2, exposure_counts=exposure_counts))
    restored = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle)
    restored_optimizer = create_optimizer(restored)
    restoration = load_checkpoint(checkpoint_path, restored, restored_optimizer,
        expected_identity=checkpoint_identity, device="cpu")
    if (semantic_digest(restored.state_dict()) != checkpoint["model_sha"]
            or restoration["optimizer_steps"] != optimizer_steps or not restored_optimizer.state):
        raise ValueError("generation checkpoint did not restore model and optimizer state")
    restored.eval()

    training_record = dict(schema="sspi-mac-pilot-generation.v2", run_id=config["run_id"],
        generation=1, theta0_sha256=baseline_sha, checkpoint=checkpoint,
        checkpoint_restore=restoration, training_games=[path.name for path in train_dirs],
        validation_games=[path.name for path in validation_dirs], search_roots=len(train_rows),
        trained_search_roots=len(selected_indices), unused_search_roots=len(train_rows) - len(selected_indices),
        training_root_selection_seed=selection_seed, training_root_indices=selected_indices,
        validation_roots=len(validation_rows), updates=updates, validation_loss=validation_metrics,
        training_outcomes=episode_outcome_counts(train_dirs),
        validation_outcomes=episode_outcome_counts(validation_dirs),
        exposure_limit=2, max_observed_exposure=max(exposure_counts),
        experiment_source_sha256=_experiment_source_sha(),
        quarantined_games=_combined_quarantine_ids(training_quarantines, validation_quarantines),
        **(dict(direct_value_stage=direct_value_stage) if direct_value_stage else {}))
    _atomic_json(run_dir / "generation-0001.json", training_record)
    _emit("checkpoint_restored", path=str(checkpoint_path), model_sha=checkpoint["model_sha"],
        optimizer_steps=optimizer_steps, validation_roots=len(validation_rows))

    if evaluation_pairs is None:
        training_record["evaluation"] = None
        _atomic_json(run_dir / "generation-0001.json", training_record)
        _write_status(run_dir, "complete", "generation_complete", generation=1,
            training_record="generation-0001.json", evaluation_record=None)
        _emit("generation_training_complete", generation=1,
            checkpoint=str(checkpoint_path), model_sha=checkpoint["model_sha"],
            optimizer_steps=restoration["optimizer_steps"], search_roots=len(train_rows),
            evaluation_deferred=True)
        return dict(training=training_record, evaluation=None)

    evaluation_record = _run_paired_evaluation(run_config=config, run_dir=run_dir,
        generation=1, trained_model=restored, trained_model_sha=checkpoint["model_sha"],
        theta0_model=baseline, theta0_sha=baseline_sha,
        checkpoint_sha=checkpoint["file_sha"], actual_port=actual_port,
        belief_port=belief_port, transport=_configured_transport(config),
        evaluation_pairs=evaluation_pairs, output_name="evaluation-paired-generation-0001.json")
    training_record["evaluation"] = dict(path="evaluation-paired-generation-0001.json",
        score=evaluation_record["score"], games=evaluation_record["protocol"]["games"])
    _atomic_json(run_dir / "generation-0001.json", training_record)
    _write_status(run_dir, "complete", "finished", generation=1,
        training_record="generation-0001.json",
        evaluation_record="evaluation-paired-generation-0001.json")
    _emit("postgame_pipeline_complete", score=evaluation_record["score"],
        games=evaluation_record["protocol"]["games"],
        checkpoint=str(checkpoint_path))
    return dict(training=training_record, evaluation=evaluation_record)


def main():
    parser = argparse.ArgumentParser(description="Continue an SSPI Mac pilot after a live episode")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--first-episode-dir", type=Path)
    parser.add_argument("--wait-pid", type=int)
    generation_mode = parser.add_mutually_exclusive_group()
    generation_mode.add_argument("--parent-generation", type=int)
    generation_mode.add_argument("--evaluate-generation", type=int)
    parser.add_argument("--actual-port", type=int, default=26789)
    parser.add_argument("--belief-port", type=int, default=26790)
    parser.add_argument("--train-games", type=int, default=16)
    parser.add_argument("--validation-games", type=int, default=4)
    parser.add_argument("--evaluation-pairs", type=int, default=32)
    parser.add_argument("--skip-evaluation", action="store_true",
        help="Defer no-search evaluation to a pre-registered later generation")
    parser.add_argument("--poll-seconds", type=float, default=15.)
    args = parser.parse_args()
    config_path = args.run_dir / "run_config.json"
    if config_path.exists() and json.loads(config_path.read_text()).get("schema") != "sspi-mac-pilot-run.v2":
        raise ValueError("old SSPI run protocol differs; preserve its evidence unchanged")
    try:
        if args.evaluate_generation is not None:
            if args.skip_evaluation:
                parser.error("--skip-evaluation cannot be combined with --evaluate-generation")
            record = evaluate_generation(run_dir=args.run_dir,
                generation=args.evaluate_generation, actual_port=args.actual_port,
                belief_port=args.belief_port, evaluation_pairs=args.evaluation_pairs)
            _write_status(args.run_dir, "complete", "generation_evaluation_complete",
                generation=args.evaluate_generation,
                evaluation_record=f"evaluation-paired-generation-{args.evaluate_generation:04d}.json")
            _emit("generation_evaluation_complete", generation=args.evaluate_generation,
                score=record["score"], games=record["protocol"]["games"])
        elif args.parent_generation is not None:
            run_next_generation(run_dir=args.run_dir, parent_generation=args.parent_generation,
                actual_port=args.actual_port, belief_port=args.belief_port,
                train_games=args.train_games, validation_games=args.validation_games,
                evaluation_pairs=None if args.skip_evaluation else args.evaluation_pairs)
        else:
            if args.first_episode_dir is None or args.wait_pid is None:
                parser.error("initial postgame run requires --first-episode-dir and --wait-pid")
            _write_status(args.run_dir, "waiting", "natural_terminal", wait_pid=args.wait_pid,
                episode_dir=str(args.first_episode_dir))
            run_postgame(run_dir=args.run_dir, first_episode_dir=args.first_episode_dir,
                wait_pid=args.wait_pid, actual_port=args.actual_port, belief_port=args.belief_port,
                train_games=args.train_games, validation_games=args.validation_games,
                evaluation_pairs=None if args.skip_evaluation else args.evaluation_pairs,
                poll_seconds=args.poll_seconds)
    except BaseException as error:
        run_dir = args.run_dir
        _atomic_json(run_dir / "postgame_status.json", dict(status="failed",
            exception=type(error).__name__, reason=str(error)))
        _emit("postgame_pipeline_failed", error=type(error).__name__, reason=str(error))
        raise


if __name__ == "__main__":
    main()
