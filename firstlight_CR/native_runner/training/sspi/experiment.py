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
from native_runner.training.sspi.collector import collect_episode
from native_runner.training.sspi.augmentation import TEMPORAL_AUX_WEIGHT, TEMPORAL_SCHEMA
from native_runner.training.sspi.learning import (
    EFFECTIVE_BATCH,
    create_optimizer,
    load_checkpoint,
    load_episode_training_rows,
    load_episode_validation_rows,
    save_checkpoint,
    search_root_loss,
    train_generation_step,
)
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.v4.factory import production_semantic_bundle


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


def paired_score_interval(pair_scores, *, seed, replicates=10_000):
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
    if metadata.get("status") != "complete" or metadata.get("training_admissible") is not True:
        raise RuntimeError("active collector did not publish a training-admissible natural terminal")
    return metadata


def _episode_model_sha(metadata):
    identity = metadata.get("identity", {})
    by_owner = identity.get("model_sha256_by_owner")
    if isinstance(by_owner, dict) and by_owner.get("0") == by_owner.get("1"):
        return by_owner["0"]
    return identity.get("model_sha256")


def _validate_episode(metadata, *, run_config, model_sha, episode_kind, search_probability):
    identity = metadata.get("identity", {})
    if (metadata.get("status") != "complete"
            or metadata.get("episode_kind", "training") != episode_kind
            or metadata.get("training_admissible", True) is not (episode_kind == "training")
            or metadata.get("search_probability") != search_probability
            or identity.get("run_id") != run_config["run_id"]
            or identity.get("arm") != run_config["arm"]
            or identity.get("seed") != run_config["seed"]
            or identity.get("device") != "cpu"
            or (episode_kind != "evaluation" and _episode_model_sha(metadata) != model_sha)):
        raise ValueError(f"episode identity differs from the expected {episode_kind} protocol")


def _write_status(run_dir, status, stage, **fields):
    _atomic_json(Path(run_dir) / "postgame_status.json",
        dict(status=status, stage=stage, **fields))


def _collect_one(*, run_config, run_dir, game_id, episode_kind, model, device,
        actual_port, belief_port, transport):
    output = run_dir / "episodes" / game_id
    shared_model = None if isinstance(model, dict) else model
    owner_models = model if isinstance(model, dict) else None
    episode = collect_episode(actual_port=actual_port, belief_port=belief_port,
        output_dir=output, run_id=run_config["run_id"], arm=run_config["arm"],
        game_id=game_id, seed=run_config["seed"], model=shared_model,
        transport=transport, episode_kind=episode_kind,
        search_probability=0. if episode_kind == "evaluation" else 0.05,
        temporal_augmentation=run_config.get("temporal_augmentation", True),
        models_by_owner=owner_models, device=device,
        progress=lambda row: print(json.dumps(row, sort_keys=True, allow_nan=False), flush=True))
    return output, episode


def run_postgame(*, run_dir, first_episode_dir, wait_pid, actual_port=26789,
        belief_port=26790, train_games=16, validation_games=4, evaluation_pairs=32,
        poll_seconds=15.):
    run_dir = Path(run_dir)
    first_episode_dir = Path(first_episode_dir)
    config = json.loads((run_dir / "run_config.json").read_text())
    if (config.get("schema") != "sspi-mac-pilot-run.v1" or config.get("status") != "collecting"
            or config.get("arm") != "A_5PCT_128" or config.get("device") != "cpu"
            or train_games < 1 or validation_games < 1 or evaluation_pairs < 1):
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

    train_dirs = [first_episode_dir]
    train_rows = load_episode_training_rows(first_episode_dir)
    _write_status(run_dir, "running", "training_collection", games=len(train_dirs),
        search_roots=len(train_rows), target_games=train_games)
    while len(train_dirs) < train_games or len(train_rows) < EFFECTIVE_BATCH:
        index = len(train_dirs) + 1
        game_id = f"A-game-{index:04d}"
        directory, metadata = _collect_one(run_config=config, run_dir=run_dir,
            game_id=game_id, episode_kind="training", model=baseline, device="cpu",
            actual_port=actual_port, belief_port=belief_port, transport=config["transport"])
        if _episode_model_sha(metadata) != baseline_sha:
            raise ValueError("frozen training generation changed theta0 between episodes")
        _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
            episode_kind="training", search_probability=0.05)
        train_dirs.append(directory)
        train_rows.extend(load_episode_training_rows(directory))
        _emit("training_collection_progress", games=len(train_dirs), search_roots=len(train_rows),
            required_games=train_games, effective_batch=EFFECTIVE_BATCH)
        _write_status(run_dir, "running", "training_collection", games=len(train_dirs),
            search_roots=len(train_rows), target_games=train_games)

    validation_dirs, validation_rows = [], []
    _write_status(run_dir, "running", "validation_collection", games=0,
        target_games=validation_games)
    while len(validation_dirs) < validation_games:
        index = len(validation_dirs) + 1
        game_id = f"V-game-{index:04d}"
        directory, metadata = _collect_one(run_config=config, run_dir=run_dir,
            game_id=game_id, episode_kind="validation", model=baseline, device="cpu",
            actual_port=actual_port, belief_port=belief_port, transport=config["transport"])
        _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
            episode_kind="validation", search_probability=0.05)
        validation_dirs.append(directory)
        validation_rows.extend(load_episode_validation_rows(directory))
        _emit("validation_collection_progress", games=len(validation_dirs),
            search_roots=len(validation_rows), required_games=validation_games)
        _write_status(run_dir, "running", "validation_collection", games=len(validation_dirs),
            search_roots=len(validation_rows), target_games=validation_games)

    schedule = exposure_batches(train_rows, seed=config["seed"], exposure_limit=2)
    _write_status(run_dir, "running", "training", games=len(train_dirs),
        search_roots=len(train_rows), updates=len(schedule))
    model = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle)
    optimizer = create_optimizer(model)
    exposure_counts = [0] * len(train_rows)
    updates = []
    for step, tokens in enumerate(schedule, start=1):
        batch_indices = [index for index, _exposure in tokens]
        for index in batch_indices:
            exposure_counts[index] += 1
        update = train_generation_step(model, optimizer, [train_rows[index] for index in batch_indices])
        updates.append(update)
        _emit("optimizer_update", step=step, updates_available=len(schedule), **update)
    if not updates or max(exposure_counts, default=0) > 2:
        raise ValueError("generation produced no valid update or exceeded root exposure cap")

    model.eval()
    with torch.no_grad():
        validation = search_root_loss(model, validation_rows)
    validation_metrics = {key: float(value.detach().cpu()) if isinstance(value, torch.Tensor) else value
        for key, value in validation.items()}
    train_episode_shas = {path.name: hashlib.sha256((path / "episode.json").read_bytes()).hexdigest()
        for path in train_dirs}
    validation_episode_shas = {path.name: hashlib.sha256((path / "episode.json").read_bytes()).hexdigest()
        for path in validation_dirs}
    checkpoint_identity = dict(run_id=config["run_id"], generation=1, seed=config["seed"],
        architecture_sha=model.architecture_sha, initialization_model_sha=baseline_sha,
        train_episode_sha256=train_episode_shas, validation_episode_sha256=validation_episode_shas,
        source_sha256=sorted({json.loads((path / "episode.json").read_text())["identity"]["source_sha256"]
            for path in train_dirs + validation_dirs}),
        action_schema="sspi-tagged-action.v1", training_protocol="5pct-x-128-search-roots.v1",
        continuation_protocol=dict(schema=TEMPORAL_SCHEMA,
            source_weight_with_samples=1. - TEMPORAL_AUX_WEIGHT,
            auxiliary_group_weight=TEMPORAL_AUX_WEIGHT,
            training_rows=sum(len(row.continuations) for row in train_rows),
            validation_rows=sum(len(row.continuations) for row in validation_rows)))
    checkpoint_path = run_dir / "checkpoints" / "generation-0001.pt"
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer, identity=checkpoint_identity,
        optimizer_steps=len(updates), run_state=dict(training_games=[path.name for path in train_dirs],
            validation_games=[path.name for path in validation_dirs], search_roots=len(train_rows),
            exposure_limit=2, exposure_counts=exposure_counts))
    restored = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle)
    restored_optimizer = create_optimizer(restored)
    restoration = load_checkpoint(checkpoint_path, restored, restored_optimizer,
        expected_identity=checkpoint_identity, device="cpu")
    if (semantic_digest(restored.state_dict()) != checkpoint["model_sha"]
            or restoration["optimizer_steps"] != len(updates) or not restored_optimizer.state):
        raise ValueError("generation checkpoint did not restore model and optimizer state")
    restored.eval()

    training_record = dict(schema="sspi-mac-pilot-generation.v1", run_id=config["run_id"],
        generation=1, theta0_sha256=baseline_sha, checkpoint=checkpoint,
        checkpoint_restore=restoration, training_games=[path.name for path in train_dirs],
        validation_games=[path.name for path in validation_dirs], search_roots=len(train_rows),
        validation_roots=len(validation_rows), updates=updates, validation_loss=validation_metrics,
        exposure_limit=2, max_observed_exposure=max(exposure_counts))
    _atomic_json(run_dir / "generation-0001.json", training_record)
    _emit("checkpoint_restored", path=str(checkpoint_path), model_sha=checkpoint["model_sha"],
        optimizer_steps=len(updates), validation_roots=len(validation_rows))

    evaluation_root = run_dir / "episodes"
    pair_scores, game_results = [], []
    _write_status(run_dir, "running", "evaluation", games=0, target_games=evaluation_pairs * 2)
    for pair_index in range(evaluation_pairs):
        scores = []
        for side in (0, 1):
            game_index = pair_index * 2 + side + 1
            game_id = f"E-game-{game_index:04d}"
            trained_owner = side
            models = {trained_owner: restored, 1 - trained_owner: baseline}
            directory, metadata = _collect_one(run_config=config, run_dir=run_dir,
                game_id=game_id, episode_kind="evaluation", model=models, device="cpu",
                actual_port=actual_port, belief_port=belief_port, transport=config["transport"])
            if metadata["search_roots"] != 0 or metadata["search_probability"] != 0.:
                raise ValueError("no-search evaluation unexpectedly ran MCTS")
            _validate_episode(metadata, run_config=config, model_sha=baseline_sha,
                episode_kind="evaluation", search_probability=0.)
            winner = metadata["result"]["winner"]
            score = 0.5 if winner is None else 1. if winner == trained_owner else 0.
            scores.append(score)
            game_results.append(dict(game_id=game_id, trained_owner=trained_owner,
                winner=winner, score=score, episode=str(directory.relative_to(evaluation_root.parent))))
            _emit("evaluation_game_complete", game_id=game_id, pair=pair_index + 1,
                games=len(game_results), total_games=evaluation_pairs * 2, winner=winner,
                trained_owner=trained_owner, score=score)
            _write_status(run_dir, "running", "evaluation", games=len(game_results),
                target_games=evaluation_pairs * 2, completed_pairs=len(pair_scores))
        pair_scores.append(sum(scores) / 2.)

    interval = paired_score_interval(pair_scores, seed=config["seed"] ^ 0x53535049)
    evaluation_record = dict(schema="sspi-mac-pilot-evaluation.v1", run_id=config["run_id"],
        generation=1, trained_checkpoint_sha256=checkpoint["file_sha"], theta0_sha256=baseline_sha,
        protocol=dict(search_probability=0., temperature=1., paired_seeds=evaluation_pairs,
            games=len(game_results), games_per_seed=2, deck=list(config["deck"])),
        pair_scores=pair_scores, score=interval, results=game_results)
    _atomic_json(run_dir / "evaluation.json", evaluation_record)
    _write_status(run_dir, "complete", "finished", generation=1,
        training_record="generation-0001.json", evaluation_record="evaluation.json")
    _emit("postgame_pipeline_complete", score=interval, games=len(game_results),
        checkpoint=str(checkpoint_path))
    return dict(training=training_record, evaluation=evaluation_record)


def main():
    parser = argparse.ArgumentParser(description="Continue an SSPI Mac pilot after a live episode")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--first-episode-dir", type=Path, required=True)
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--actual-port", type=int, default=26789)
    parser.add_argument("--belief-port", type=int, default=26790)
    parser.add_argument("--train-games", type=int, default=16)
    parser.add_argument("--validation-games", type=int, default=4)
    parser.add_argument("--evaluation-pairs", type=int, default=32)
    parser.add_argument("--poll-seconds", type=float, default=15.)
    args = parser.parse_args()
    try:
        _write_status(args.run_dir, "waiting", "natural_terminal", wait_pid=args.wait_pid,
            episode_dir=str(args.first_episode_dir))
        run_postgame(run_dir=args.run_dir, first_episode_dir=args.first_episode_dir,
            wait_pid=args.wait_pid, actual_port=args.actual_port, belief_port=args.belief_port,
            train_games=args.train_games, validation_games=args.validation_games,
            evaluation_pairs=args.evaluation_pairs, poll_seconds=args.poll_seconds)
    except BaseException as error:
        run_dir = args.run_dir
        _atomic_json(run_dir / "postgame_status.json", dict(status="failed",
            exception=type(error).__name__, reason=str(error)))
        _emit("postgame_pipeline_failed", error=type(error).__name__, reason=str(error))
        raise


if __name__ == "__main__":
    main()
