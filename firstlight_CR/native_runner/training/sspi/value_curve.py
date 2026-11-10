"""Learning-curve experiment for direct-policy, value-head-only SSPI training."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import traceback

import torch

from ..fair_search.contracts import semantic_digest
from ..v4.factory import production_semantic_bundle
from .collector import (VALUE_ARM, VALUE_EPISODE_SCHEMA, _source_sha, collect_episode,
    episode_seed_identity, preflight_native_endpoints)
from .learning import CHECKPOINT_SCHEMA, decisive_result, load_checkpoint, save_checkpoint
from .model import initialize_fresh_model
from .value_learning import (create_value_optimizer, encode_value_head_inputs,
    load_value_episode, policy_function_sha, policy_head_sha, prediction_metrics,
    train_value_curve_batch, value_head_predictions)


RUN_SCHEMA = "sspi-value-learning-curve.v1"
CHECK_SCHEMA = "sspi-value-learning-curve-check.v1"
HOLDOUT_SCHEMA = "sspi-value-learning-curve-holdout.v1"
SEED = 20261103
CHECK_EVERY_GAMES = 64
MAX_TRAIN_GAMES = 1024
VALIDATION_GAMES = 32
HOLDOUT_GAMES = 32
VALUE_SAMPLE_PROBABILITY = 0.5
VALUE_HEAD_PARAMETERS = 262_914
TRAIN_BATCH_SIZE = 128
TRAIN_MICROBATCH_SIZE = 4
LEARNING_RATE = 1e-4
WEIGHT_DECAY = 1e-4
GRADIENT_CLIP = 1.
BOOTSTRAP_REPLICATES = 20_000
FAMILYWISE_ALPHA = 0.05
SEED_DOMAINS = {"training": 0x100000, "validation": 0x200000, "holdout": 0x300000}


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_torch(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
            suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        torch.save(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _model_sha(model):
    return semantic_digest(model.state_dict())


def _source_identity():
    project_root = Path(__file__).resolve().parents[3]
    files = sorted((project_root / "native_runner" / "training").rglob("*.py"))
    files.extend(sorted((project_root / "research" / "puct-runtime").rglob("*.py")))
    return semantic_digest({path.relative_to(project_root).as_posix(): _file_sha(path)
        for path in files})


def _seed_for(split, index):
    if split not in SEED_DOMAINS or type(index) is not int or index < 1:
        raise ValueError("value-curve episode seed requires a known split and positive index")
    return SEED + SEED_DOMAINS[split] + index


def _game_id(split, index):
    if split not in SEED_DOMAINS or type(index) is not int or index < 1:
        raise ValueError("invalid value-curve game identity")
    prefix = {"training": "train", "validation": "validation", "holdout": "holdout"}[split]
    return f"{prefix}-{index:04d}"


def _random_stream_id(run_id, split, index):
    return f"{run_id}:{split}:{index:04d}"


def _log_line(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _new_model(device="cpu"):
    return initialize_fresh_model(SEED, device=device, bundle=production_semantic_bundle())


def _check_head_scope(model):
    actual = sum(parameter.numel() for parameter in model.value_head.parameters())
    if model.schema != "sspi-policy-value.v4" or actual != VALUE_HEAD_PARAMETERS:
        raise ValueError("v4 value-head architecture or parameter count differs")
    return actual


def _load_checkpoint_model(path, identity, *, device):
    model = _new_model(device="cpu")
    _check_head_scope(model)
    optimizer = create_value_optimizer(model, head_only=True)
    restored = load_checkpoint(path, model, optimizer,
        expected_identity=identity, device=device)
    if restored["model_sha"] != _model_sha(model):
        raise ValueError("value-curve checkpoint failed model restoration")
    return model, optimizer, restored


def _load_checkpoint_weights(path, identity, *, device):
    model = _new_model(device="cpu")
    _check_head_scope(model)
    payload = _read_checkpoint_payload(path, identity)
    if (payload.get("architecture") != model.architecture
            or payload.get("architecture_sha") != model.architecture_sha):
        raise ValueError("value-curve checkpoint architecture differs from v4")
    model.load_state_dict(payload["model"], strict=True)
    model.to(device).eval()
    return model, dict(file_sha=_file_sha(path), model_sha=payload["model_sha"])


def _read_checkpoint_payload(path, expected_identity):
    envelope = torch.load(path, weights_only=True, map_location="cpu")
    if (not isinstance(envelope, dict)
            or envelope.get("schema") != CHECKPOINT_SCHEMA
            or semantic_digest(envelope.get("payload")) != envelope.get("payload_sha")):
        raise ValueError("value-curve checkpoint envelope checksum differs")
    payload = envelope["payload"]
    if (payload.get("schema") != CHECKPOINT_SCHEMA
            or payload.get("identity") != expected_identity
            or payload.get("identity_sha") != semantic_digest(expected_identity)
            or payload.get("model_sha") != semantic_digest(payload.get("model"))
            or payload.get("optimizer_sha") != semantic_digest(payload.get("optimizer"))
            or payload.get("run_state_sha") != semantic_digest(payload.get("run_state"))):
        raise ValueError("value-curve checkpoint identity or model checksum differs")
    return payload


def _episode_source(config, split, index):
    revisions = [item for item in config.get("collector_source_revisions", ())
        if item["split"] == split and item["start"] <= index <= item["end"]]
    if len(revisions) > 1:
        raise ValueError("collector source revisions overlap at one episode")
    return revisions[0]["source_sha256"] if revisions else config["collector_source_sha"]


def _validate_episode(directory, config, split, index):
    directory = Path(directory)
    metadata_path = directory / "episode.json"
    if (directory / "quarantine.json").exists() or not metadata_path.is_file():
        raise RuntimeError(f"value-curve episode is incomplete or quarantined: {directory}")
    metadata = json.loads(metadata_path.read_text())
    expected_kind = "training" if split == "training" else "validation"
    expected_game_id = _game_id(split, index)
    expected_seed = _seed_for(split, index)
    stream = _random_stream_id(config["run_id"], split, index)
    identity = metadata.get("identity") or {}
    expected_seeds = episode_seed_identity(expected_seed, run_id=config["run_id"],
        arm=VALUE_ARM, game_id=expected_game_id, random_stream_id=stream)
    decisive = decisive_result(metadata.get("result"))
    if (metadata.get("schema") != VALUE_EPISODE_SCHEMA
            or metadata.get("status") != "complete"
            or metadata.get("episode_kind") != expected_kind
            or metadata.get("training_admissible") is not (split == "training" and decisive)
            or metadata.get("search_roots") != 0
            or metadata.get("search_probability") != 0.
            or metadata.get("value_sample_probability") != VALUE_SAMPLE_PROBABILITY
            or identity.get("collection_protocol") != "value-only.v2"
            or identity.get("run_id") != config["run_id"]
            or identity.get("game_id") != expected_game_id
            or identity.get("model_sha256_by_owner") != {
                "0": config["initial_model_sha"], "1": config["initial_model_sha"]}
            or identity.get("source_sha256") != _episode_source(config, split, index)
            or identity.get("engine_sha256") != config["engine_sha256"]
            or identity.get("native_transport") != config["transport"]
            or identity.get("value_sample_probability") != VALUE_SAMPLE_PROBABILITY
            or identity.get("random_stream_id") != stream
            or any(identity.get(key) != value for key, value in expected_seeds.items())):
        raise ValueError("episode does not match the frozen v4 value-curve identity")
    rows, loaded = load_value_episode(directory, expected_kind=expected_kind,
        run_id=config["run_id"], model_sha=config["initial_model_sha"])
    if loaded.get("result") != metadata.get("result"):
        raise ValueError("episode terminal result changed while loading samples")
    keys = [(row.game_id, row.owner, row.decision_index) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("episode contains duplicate value decision states")
    return rows, metadata


def _collect_one(run_dir, config, actor, split, index, *, ports, transport):
    game_id = _game_id(split, index)
    destination = Path(run_dir) / "episodes" / game_id
    if not destination.exists():
        def progress(row):
            _log_line(Path(run_dir) / "collector.log", row)
        kind = "training" if split == "training" else "validation"
        collect_episode(actual_port=ports["actual"], belief_port=ports["belief_owner_0"],
            belief_ports={0: ports["belief_owner_0"], 1: ports["belief_owner_1"]},
            output_dir=destination, run_id=config["run_id"], arm=VALUE_ARM,
            game_id=game_id, seed=_seed_for(split, index), model=actor, device="cpu",
            transport=transport, episode_kind=kind, search_probability=0.,
            temporal_augmentation=False,
            random_stream_id=_random_stream_id(config["run_id"], split, index),
            value_sample_probability=VALUE_SAMPLE_PROBABILITY, progress=progress)
    rows, metadata = _validate_episode(destination, config, split, index)
    return destination, rows, metadata


def _constant_predictions(rows, counts):
    if (not isinstance(counts, (list, tuple)) or len(counts) != 2
            or any(type(value) is not int or value < 0 for value in counts)
            or sum(counts) == 0):
        raise ValueError("training-frequency baseline requires decisive training labels")
    total = float(sum(counts))
    probabilities = (counts[0] / total, counts[1] / total)
    return [(row.game_id, int(row.final_wl), probabilities) for row in rows]


def _curve_metrics(predictions):
    result = prediction_metrics(predictions)
    groups = {}
    for row in predictions:
        groups.setdefault(row[0], []).append(row)
    metrics = [prediction_metrics(rows) for rows in groups.values()]
    result["equal_game_mean"] = {key: sum(row[key] for row in metrics) / len(metrics)
        for key in ("wl_ce", "brier")}
    result["aggregation"] = "sample-means-with-equal-game-CE-and-Brier-reported-separately"
    return result


def _paired_game_bootstrap(candidate, baseline, *, seed, alpha, replicates=BOOTSTRAP_REPLICATES):
    if (not candidate or len(candidate) != len(baseline)
            or any(a[:2] != b[:2] for a, b in zip(candidate, baseline))
            or type(seed) is not int or not 0. < alpha < 1.):
        raise ValueError("paired value bootstrap requires matching predictions and alpha")
    by_game = {}
    for current, reference in zip(candidate, baseline):
        game_id, label, current_probabilities = current
        reference_probabilities = reference[2]
        delta = (math.log(max(reference_probabilities[label], 1e-12))
            - math.log(max(current_probabilities[label], 1e-12)))
        total, count = by_game.get(game_id, (0., 0))
        by_game[game_id] = (total + delta, count + 1)
    games = sorted(by_game)
    if len(games) < 2:
        raise ValueError("paired value bootstrap requires at least two complete games")
    game_means = [by_game[game][0] / by_game[game][1] for game in games]
    rng = random.Random(seed)
    samples = [sum(game_means[rng.randrange(len(games))] for _ in games) / len(games)
        for _ in range(replicates)]
    samples.sort()
    upper_index = min(replicates - 1, max(0, math.ceil((1. - alpha) * (replicates + 1)) - 1))
    lower_index = min(replicates - 1, max(0, math.floor(alpha * (replicates + 1)) - 1))
    return dict(mean=sum(game_means) / len(games), upper=samples[upper_index],
        lower=samples[lower_index],
        ci95=[samples[int(.025 * (replicates - 1))], samples[int(.975 * (replicates - 1))]],
        games=len(games), replicates=replicates, one_sided_alpha=alpha,
        method="paired-complete-game-bootstrap-equal-game-mean", seed=seed)


def _evaluate(model, rows, features, initial_model, training_counts, *, check_index):
    tests_per_family = (MAX_TRAIN_GAMES // CHECK_EVERY_GAMES) * 2
    adjusted_alpha = FAMILYWISE_ALPHA / tests_per_family
    correction = dict(familywise_alpha=FAMILYWISE_ALPHA,
        checks=MAX_TRAIN_GAMES // CHECK_EVERY_GAMES, baselines=2,
        one_sided_test_alpha=adjusted_alpha)
    if not rows:
        return dict(status="insufficient_validation_data", check_index=check_index,
            current=None, initial=None, training_frequency_constant=None,
            current_minus_initial_ce=None, current_minus_constant_ce=None,
            repeated_test_correction=correction, validation_signal=False,
            reason="no_decisive_sampled_validation_states")

    current_predictions = value_head_predictions(model, rows, features)
    initial_predictions = value_head_predictions(initial_model, rows, features)
    current_metrics = _curve_metrics(current_predictions)
    initial_metrics = _curve_metrics(initial_predictions)
    complete_games = len({row.game_id for row in rows})
    current_initial = (_paired_game_bootstrap(current_predictions, initial_predictions,
            seed=SEED ^ check_index ^ 0x494E4954, alpha=adjusted_alpha)
        if complete_games >= 2 else None)
    has_training_labels = sum(training_counts) > 0
    constant_predictions = (_constant_predictions(rows, training_counts)
        if has_training_labels else None)
    constant_metrics = (_curve_metrics(constant_predictions)
        if constant_predictions is not None else None)
    current_constant = (_paired_game_bootstrap(current_predictions, constant_predictions,
            seed=SEED ^ check_index ^ 0x434F4E53, alpha=adjusted_alpha)
        if complete_games >= 2 and constant_predictions is not None else None)
    evaluated = current_initial is not None and current_constant is not None
    status = ("evaluated" if evaluated else "insufficient_complete_games"
        if complete_games < 2 else "insufficient_training_labels")
    reasons = []
    if complete_games < 2:
        reasons.append("fewer_than_two_decisive_validation_games")
    if constant_predictions is None:
        reasons.append("no_decisive_training_labels_for_frequency_baseline")
    return dict(status=status, check_index=check_index,
        current=current_metrics, initial=initial_metrics,
        training_frequency_constant=constant_metrics,
        current_minus_initial_ce=current_initial,
        current_minus_constant_ce=current_constant,
        repeated_test_correction=correction,
        validation_signal=(evaluated and current_initial["upper"] < 0.
            and current_constant["upper"] < 0.),
        reasons=reasons)


def _row_identity(row):
    return (row.game_id, row.owner, row.decision_index, row.fair_sha256,
        int(row.final_wl))


def _load_split(run_dir, config, split, count):
    rows, games, draws, episode_shas = [], 0, 0, {}
    for index in range(1, count + 1):
        directory = Path(run_dir) / "episodes" / _game_id(split, index)
        episode_rows, metadata = _validate_episode(directory, config, split, index)
        rows.extend(episode_rows)
        games += 1
        draws += int(not decisive_result(metadata["result"]))
        episode_shas[_game_id(split, index)] = _file_sha(directory / "episode.json")
    return rows, dict(collected_games=games, decisive_games=games - draws,
        excluded_draw_games=draws, selected_rows=len(rows), episode_sha256=episode_shas)


def _write_remote_training_batch(run_dir, config, batch_start, batch_end, *, device):
    run_dir = Path(run_dir)
    started = time.monotonic()
    def phase(stage, **details):
        _log_line(run_dir / "training-progress.jsonl", dict(stage=stage,
            batch_start=batch_start, batch_end=batch_end, pid=os.getpid(),
            elapsed_seconds=time.monotonic() - started, **details))
    if (type(batch_start) is not int or type(batch_end) is not int
            or batch_start < 1 or batch_end != batch_start + CHECK_EVERY_GAMES - 1
            or batch_end > MAX_TRAIN_GAMES or batch_end % CHECK_EVERY_GAMES):
        raise ValueError("training checkpoint must end on a fixed 64-game boundary")
    record_path = run_dir / "checks" / f"check-{batch_end:04d}.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        checkpoint_path = run_dir / "checkpoints" / f"curve-{batch_end:04d}.pt"
        if (record.get("schema") != CHECK_SCHEMA or record.get("run_id") != config["run_id"]
                or record.get("training_games") != batch_end
                or _file_sha(checkpoint_path) != record.get("checkpoint", {}).get("file_sha256")):
            raise ValueError("existing checkpoint record identity differs")
        _promote_checkpoint(run_dir, config, checkpoint_path, batch_end)
        return record
    if config.get("source_sha256") != _source_identity():
        raise ValueError("remote training code does not match the run source identity")
    phase("checkpoint_load", device=str(device), torch_threads=torch.get_num_threads())
    identity = config["checkpoint_identity"]
    current_path = run_dir / "checkpoints" / "current.pt"
    checkpoint_path = run_dir / "checkpoints" / f"curve-{batch_end:04d}.pt"
    if checkpoint_path.exists():
        return _recover_saved_check(run_dir, config, checkpoint_path, batch_end, device=device)
    model, optimizer, restored = _load_checkpoint_model(current_path, identity, device=device)
    prior_state = restored["run_state"]
    prior_games = prior_state.get("training_games", 0)
    if prior_games != batch_start - 1:
        raise ValueError("remote checkpoint training cursor differs from requested exposure range")
    if policy_function_sha(model) != config["initial_policy_function_sha"]:
        raise ValueError("value-curve checkpoint changed the frozen shared policy function")
    training_rows, batch_episode_shas, batch_decisive_games, batch_draws = [], {}, 0, 0
    for slot, index in enumerate(range(batch_start, batch_end + 1), start=1):
        game_id = _game_id("training", index)
        stage_dir = run_dir / "staging" / "training" / f"slot-{slot:04d}"
        episode_rows, metadata = _validate_episode(stage_dir,
            config, "training", index)
        training_rows.extend(episode_rows)
        batch_episode_shas[game_id] = _file_sha(stage_dir / "episode.json")
        if decisive_result(metadata["result"]):
            batch_decisive_games += 1
        else:
            batch_draws += 1
        if slot % 8 == 0:
            phase("training_episode_validation", completed=slot, rows=len(training_rows))
    training_stats = dict(collected_games=CHECK_EVERY_GAMES,
        decisive_games=batch_decisive_games, excluded_draw_games=batch_draws,
        selected_rows=len(training_rows), episode_sha256=batch_episode_shas)
    training_keys = [_row_identity(row)[:3] for row in training_rows]
    if len(training_keys) != len(set(training_keys)):
        raise ValueError("64-game value batch exposes a decision more than once")
    if {row.game_id for row in training_rows} & {f"validation-{i:04d}" for i in range(1, VALIDATION_GAMES + 1)}:
        raise ValueError("training and validation episode identities overlap")
    phase("validation_episode_load")
    validation_rows, validation_stats = _load_split(run_dir, config, "validation", VALIDATION_GAMES)
    initial_model, initial_restore = _load_checkpoint_weights(
        run_dir / "checkpoints" / "initial.pt", identity, device=device)
    if policy_function_sha(initial_model) != policy_function_sha(model):
        raise ValueError("initial and trained value-curve models have different frozen backbones")
    feature_path = run_dir / "derived" / "validation-head-features.pt"
    feature_manifest_path = run_dir / "derived" / "validation-head-features.json"
    row_manifest = [_row_identity(row) for row in validation_rows]
    if feature_path.exists() or feature_manifest_path.exists():
        if not (feature_path.is_file() and feature_manifest_path.is_file()):
            raise ValueError("validation feature cache is incomplete")
        feature_manifest = json.loads(feature_manifest_path.read_text())
        if (feature_manifest.get("run_id") != config["run_id"]
                or feature_manifest.get("backbone_sha") != config["initial_policy_function_sha"]
                or feature_manifest.get("row_identity") != [list(item) for item in row_manifest]
                or feature_manifest.get("file_sha256") != _file_sha(feature_path)):
            raise ValueError("validation feature cache identity or checksum differs")
        feature_payload = torch.load(feature_path, weights_only=True, map_location="cpu")
        if (feature_payload.get("schema") != "sspi-value-curve-features.v1"
                or feature_payload.get("backbone_sha") != config["initial_policy_function_sha"]
                or feature_payload.get("row_identity") != row_manifest):
            raise ValueError("validation feature payload differs from the fixed split")
        validation_features = feature_payload["features"].to(device)
    else:
        validation_features = (encode_value_head_inputs(initial_model, validation_rows,
                progress=lambda done, total: phase("validation_encoding", done=done, total=total))
            if validation_rows else torch.empty((0, initial_model.value_head[0].in_features),
                dtype=next(initial_model.parameters()).dtype))
        _atomic_torch(feature_path, dict(schema="sspi-value-curve-features.v1",
            backbone_sha=config["initial_policy_function_sha"], row_identity=row_manifest,
            features=validation_features.detach().cpu()))
        _atomic_json(feature_manifest_path, dict(run_id=config["run_id"],
            backbone_sha=config["initial_policy_function_sha"],
            row_identity=[list(item) for item in row_manifest], file_sha256=_file_sha(feature_path)))
    phase("training_encoding_start", rows=len(training_rows))
    training_features = (encode_value_head_inputs(model, training_rows,
            progress=lambda done, total: phase("training_encoding", done=done, total=total))
        if training_rows else None)
    phase("value_head_update")
    update = train_value_curve_batch(model, optimizer, training_rows,
        seed=SEED ^ batch_end ^ 0x56414C55, batch_size=TRAIN_BATCH_SIZE,
        microbatch_size=TRAIN_MICROBATCH_SIZE, features=training_features)
    if update["status"] not in ("updated", "skipped"):
        raise ValueError("value curve checkpoint had no valid training update")
    optimizer_steps = restored["optimizer_steps"] + update["optimizer_steps"]
    previous_counts = prior_state.get("training_label_counts", [0, 0])
    if len(previous_counts) != 2:
        raise ValueError("checkpoint training label counts are malformed")
    new_counts = [sum(int(row.final_wl) == index for row in training_rows) for index in (0, 1)]
    cumulative_counts = [previous_counts[i] + new_counts[i] for i in (0, 1)]
    check_index = batch_end // CHECK_EVERY_GAMES
    phase("validation_evaluation")
    evaluation = _evaluate(model, validation_rows, validation_features, initial_model,
        cumulative_counts, check_index=check_index)
    batch_episode_ids = [_game_id("training", index)
        for index in range(batch_start, batch_end + 1)]
    prior_ranges = list(prior_state.get("trained_game_ranges", []))
    prior_ranges.append(dict(start=batch_start, end=batch_end,
        training_rows=len(training_rows), exposure_sha256=update["exposure_sha256"],
        episode_sha256=batch_episode_shas))
    run_state = dict(training_games=batch_end,
        decisive_training_games=prior_state.get("decisive_training_games", 0) + batch_decisive_games,
        training_rows=sum(item["training_rows"] for item in prior_ranges),
        training_label_counts=cumulative_counts, optimizer_steps=optimizer_steps,
        trained_game_ranges=prior_ranges, last_validation_signal=evaluation["validation_signal"],
        last_check_index=check_index)
    summary = dict(schema=CHECK_SCHEMA, run_id=config["run_id"], check_index=check_index,
        training_games=batch_end, collected_games=batch_end,
        decisive_training_games=run_state["decisive_training_games"],
        excluded_draw_games=batch_end - run_state["decisive_training_games"],
        sampled_training_rows=run_state["training_rows"],
        current_batch=dict(update, start=batch_start, end=batch_end,
            optimizer_steps_total=optimizer_steps, training_games=training_stats,
            episode_sha256=batch_episode_shas),
        validation=validation_stats, evaluation=evaluation,
        initial_checkpoint_sha256=initial_restore["file_sha"],
        source_sha256=config["source_sha256"], device=str(device),
        status="signal" if evaluation["validation_signal"] else "continue")
    # The complete result precedes checkpoint promotion and lives inside the
    # checkpoint so a crash cannot force a second exposure of the same states.
    run_state["last_check_summary"] = summary
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer,
        identity=identity, optimizer_steps=optimizer_steps, run_state=run_state)
    verify_model, verify_optimizer, verify = _load_checkpoint_model(
        checkpoint_path, identity, device=device)
    if (verify["model_sha"] != checkpoint["model_sha"]
            or verify["optimizer_steps"] != optimizer_steps
            or verify["run_state"] != run_state
            or policy_function_sha(verify_model) != config["initial_policy_function_sha"]
            or semantic_digest(verify_optimizer.state_dict()) != semantic_digest(optimizer.state_dict())):
        raise ValueError("value-curve model and AdamW checkpoint round-trip differs")
    record = dict(summary, checkpoint=dict(path=str(checkpoint_path), file_sha256=checkpoint["file_sha"],
            model_sha=checkpoint["model_sha"], optimizer_sha256=semantic_digest(optimizer.state_dict()),
            optimizer_steps=optimizer_steps, run_state_sha256=semantic_digest(run_state)))
    _atomic_json(record_path, record)
    _promote_checkpoint(run_dir, config, checkpoint_path, batch_end)
    _atomic_json(run_dir / "status.json", dict(status="running", stage="checkpoint_complete",
        latest_check=record))
    phase("checkpoint_complete", optimizer_steps=optimizer_steps,
        validation_signal=evaluation["validation_signal"])
    return record


def _promote_checkpoint(run_dir, config, checkpoint_path, training_games):
    """Advance a committed cursor; an older result never rolls it backwards."""
    run_dir = Path(run_dir)
    payload = _read_checkpoint_payload(checkpoint_path, config["checkpoint_identity"])
    if payload["run_state"].get("training_games") != training_games:
        raise ValueError("saved checkpoint cursor differs from its check record")
    current_path = run_dir / "checkpoints" / "current.pt"
    current = _read_checkpoint_payload(current_path, config["checkpoint_identity"])
    current_games = current["run_state"].get("training_games", 0)
    if current_games >= training_games:
        if current_games == training_games and _file_sha(current_path) != _file_sha(checkpoint_path):
            raise ValueError("current and saved checkpoints disagree at the same boundary")
        return
    if current_games != training_games - CHECK_EVERY_GAMES:
        raise ValueError("checkpoint promotion would skip a training exposure boundary")
    temporary = current_path.with_name("current.pt.tmp")
    shutil.copyfile(checkpoint_path, temporary)
    os.replace(temporary, current_path)


def _recover_saved_check(run_dir, config, checkpoint_path, training_games, *, device):
    """Finish a saved transaction without issuing another optimizer step."""
    model, optimizer, restored = _load_checkpoint_model(checkpoint_path,
        config["checkpoint_identity"], device=device)
    state = restored["run_state"]
    summary = state.get("last_check_summary")
    if (not isinstance(summary, dict) or summary.get("schema") != CHECK_SCHEMA
            or summary.get("training_games") != training_games
            or summary.get("run_id") != config["run_id"]
            or policy_function_sha(model) != config["initial_policy_function_sha"]):
        raise RuntimeError("saved checkpoint predates recoverable check summaries; inspect evidence")
    record = dict(summary, checkpoint=dict(path=str(checkpoint_path),
        file_sha256=restored["file_sha"], model_sha=restored["model_sha"],
        optimizer_sha256=semantic_digest(optimizer.state_dict()),
        optimizer_steps=restored["optimizer_steps"], run_state_sha256=semantic_digest(state)))
    _atomic_json(Path(run_dir) / "checks" / f"check-{training_games:04d}.json", record)
    _promote_checkpoint(run_dir, config, checkpoint_path, training_games)
    return record


def _remote_holdout(run_dir, config, *, device):
    run_dir = Path(run_dir)
    record_path = run_dir / "final-holdout.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        if (record.get("schema") != HOLDOUT_SCHEMA or record.get("run_id") != config["run_id"]
                or record.get("checkpoint_sha256") != _file_sha(run_dir / "checkpoints" / "current.pt")):
            raise ValueError("existing final holdout identity differs")
        return record
    identity = config["checkpoint_identity"]
    model, _optimizer, restored = _load_checkpoint_model(
        run_dir / "checkpoints" / "current.pt", identity, device=device)
    run_state = restored["run_state"]
    if not run_state.get("training_games"):
        raise ValueError("final holdout requires a trained checkpoint")
    rows, stats = _load_split(run_dir, config, "holdout", HOLDOUT_GAMES)
    initial_model, _ = _load_checkpoint_weights(
        run_dir / "checkpoints" / "initial.pt", identity, device=device)
    if policy_function_sha(model) != policy_function_sha(initial_model):
        raise ValueError("final holdout model changed the fixed policy function")
    decisive_games = len({row.game_id for row in rows})
    if decisive_games < 2 or sum(run_state.get("training_label_counts", ())) == 0:
        record = dict(schema=HOLDOUT_SCHEMA, run_id=config["run_id"],
            status="not_evaluable", reason=("no_decisive_sampled_holdout_states" if not rows
                else "fewer_than_two_decisive_holdout_games" if decisive_games < 2
                else "no_decisive_training_labels_for_frequency_baseline"),
            training_games=run_state["training_games"], selected_rows=len(rows),
            decisive_holdout_games=decisive_games, games=stats,
            current=None, initial=None, training_frequency_constant=None,
            current_minus_initial_ce=None, current_minus_constant_ce=None,
            comparisons=2, familywise_alpha=FAMILYWISE_ALPHA,
            policy_function_sha=policy_function_sha(model),
            optimizer_steps=restored["optimizer_steps"],
            checkpoint_sha256=_file_sha(run_dir / "checkpoints" / "current.pt"))
        _atomic_json(record_path, record)
        _atomic_json(run_dir / "status.json", dict(status="complete",
            stage="holdout_not_evaluable", holdout=record))
        return record
    features = encode_value_head_inputs(initial_model, rows)
    current = value_head_predictions(model, rows, features)
    initial = value_head_predictions(initial_model, rows, features)
    constant = _constant_predictions(rows, run_state["training_label_counts"])
    alpha = FAMILYWISE_ALPHA / 2.
    current_initial = _paired_game_bootstrap(current, initial,
        seed=SEED ^ 0x484F4C44, alpha=alpha)
    current_constant = _paired_game_bootstrap(current, constant,
        seed=SEED ^ 0x484F4C45, alpha=alpha)
    record = dict(schema=HOLDOUT_SCHEMA, run_id=config["run_id"],
        status="confirmed" if current_initial["upper"] < 0.
            and current_constant["upper"] < 0. else "not_confirmed",
        training_games=run_state["training_games"],
        model_sha=_model_sha(model), selected_rows=len(rows), games=stats,
        current=_curve_metrics(current), initial=_curve_metrics(initial),
        training_frequency_constant=_curve_metrics(constant),
        current_minus_initial_ce=current_initial,
        current_minus_constant_ce=current_constant,
        comparisons=2, familywise_alpha=FAMILYWISE_ALPHA,
        one_sided_test_alpha=alpha,
        policy_function_sha=policy_function_sha(model),
        optimizer_steps=restored["optimizer_steps"],
        checkpoint_sha256=_file_sha(run_dir / "checkpoints" / "current.pt"))
    _atomic_json(record_path, record)
    _atomic_json(run_dir / "status.json", dict(status="complete", stage="holdout_complete",
        holdout=record))
    return record


def _remote_preflight():
    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError("remote CUDA training preflight found no CUDA device")
    free_by_device = []
    for index in range(torch.cuda.device_count()):
        try:
            free, total = torch.cuda.mem_get_info(index)
            free_by_device.append((int(free), int(total), index))
        except RuntimeError:
            continue
    if not free_by_device:
        raise RuntimeError("remote CUDA devices did not report free memory")
    free_bytes, total_bytes, selected = max(free_by_device)
    if free_bytes < 8 * 1024 ** 3:
        raise RuntimeError("no remote CUDA device has the required free-memory reserve")
    model = _new_model(device="cpu")
    _check_head_scope(model)
    frozen_sha = policy_function_sha(model)
    head_before = semantic_digest(model.value_head.state_dict())
    model.to(f"cuda:{selected}")
    optimizer = create_value_optimizer(model, head_only=True)
    features = torch.randn((8, model.value_head[0].in_features), device=f"cuda:{selected}")
    labels = torch.arange(8, device=f"cuda:{selected}", dtype=torch.long) % 2
    optimizer.zero_grad(set_to_none=True)
    for _ in range(2):
        torch.nn.functional.cross_entropy(model.value_head(features), labels).backward()
        torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.)
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
    torch.cuda.synchronize(selected)
    if (semantic_digest(model.value_head.state_dict()) == head_before
            or policy_function_sha(model) != frozen_sha):
        raise RuntimeError("remote CUDA value-head update or freeze check failed")
    return dict(status="ready", torch_version=torch.__version__, cuda_version=torch.version.cuda,
        device_count=torch.cuda.device_count(), selected_physical_device=selected,
        device_name=torch.cuda.get_device_name(selected), free_bytes=free_bytes,
        total_bytes=total_bytes, value_head_parameters=VALUE_HEAD_PARAMETERS,
        training_device=f"cuda:{selected}", two_step_update="passed",
        frozen_policy_function="passed")


def _ssh(host, command, *, capture=True):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, command],
        text=True, capture_output=capture)
    if result.returncode:
        detail = ((result.stderr or "") + "\n" + (result.stdout or "")) if capture else ""
        raise RuntimeError(f"remote command exited {result.returncode}: {detail[-16000:]}")
    return result.stdout.strip() if capture else ""


def _sync_tar_to_remote(host, local_root, members, remote_dir):
    command = f"mkdir -p {shlex.quote(str(remote_dir))} && tar -xf - -C {shlex.quote(str(remote_dir))}"
    tar_env = os.environ.copy()
    tar_env["COPYFILE_DISABLE"] = "1"
    excludes = ("--exclude=__pycache__", "--exclude=*.pyc", "--exclude=*.so",
        "--exclude=*.apk", "--exclude=*.pt", "--exclude=*.pth", "--exclude=*.bin")
    tar_process = subprocess.Popen(["tar", "-cf", "-", *excludes,
        "-C", str(local_root), *members],
        stdout=subprocess.PIPE, env=tar_env)
    ssh_process = subprocess.Popen(["ssh", host, command], stdin=tar_process.stdout)
    tar_process.stdout.close()
    ssh_status = ssh_process.wait()
    tar_status = tar_process.wait()
    if ssh_status or tar_status:
        raise subprocess.CalledProcessError(ssh_status or tar_status, "tar | ssh")


def _sync_episode_dirs(host, local_run_dir, remote_run_dir, game_ids):
    if not game_ids:
        return
    local_root = Path(local_run_dir) / "episodes"
    members = [f"episodes/{game_id}" for game_id in game_ids]
    command = (f"mkdir -p {shlex.quote(str(PurePosixPath(remote_run_dir) / 'episodes'))} "
        f"&& tar -xf - -C {shlex.quote(str(remote_run_dir))}")
    tar_env = os.environ.copy()
    tar_env["COPYFILE_DISABLE"] = "1"
    tar_process = subprocess.Popen(["tar", "-cf", "-", "-C", str(local_root.parent), *members],
        stdout=subprocess.PIPE, env=tar_env)
    ssh_process = subprocess.Popen(["ssh", host, command], stdin=tar_process.stdout)
    tar_process.stdout.close()
    ssh_status = ssh_process.wait()
    tar_status = tar_process.wait()
    if ssh_status or tar_status:
        raise subprocess.CalledProcessError(ssh_status or tar_status, "episode tar | ssh")


def _sync_training_batch(host, local_run_dir, remote_run_dir, batch_start, batch_end):
    local_root = Path(local_run_dir)
    members = [f"episodes/{_game_id('training', index)}"
        for index in range(batch_start, batch_end + 1)]
    transforms = []
    for slot, index in enumerate(range(batch_start, batch_end + 1), start=1):
        source = f"episodes/{_game_id('training', index)}"
        target = f"staging/training/slot-{slot:04d}"
        expression = f"s,^" + source + f",{target},"
        transforms.append("--transform=" + shlex.quote(expression))
    command = (f"mkdir -p {shlex.quote(remote_run_dir + '/staging/training')} "
        f"&& tar -xf - -C {shlex.quote(remote_run_dir)} " + " ".join(transforms))
    tar_env = os.environ.copy()
    tar_env["COPYFILE_DISABLE"] = "1"
    tar_process = subprocess.Popen(["tar", "-cf", "-", "-C", str(local_root), *members],
        stdout=subprocess.PIPE, env=tar_env)
    ssh_process = subprocess.Popen(["ssh", host, command], stdin=tar_process.stdout)
    tar_process.stdout.close()
    ssh_status = ssh_process.wait()
    tar_status = tar_process.wait()
    if ssh_status or tar_status:
        raise subprocess.CalledProcessError(ssh_status or tar_status, "training batch tar | ssh")


def _scp_to_remote(host, local_path, remote_path):
    subprocess.run(["scp", str(local_path), f"{host}:{remote_path}"], check=True)


def _scp_from_remote(host, remote_path, local_path):
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["scp", f"{host}:{remote_path}", str(local_path)], check=True)


def _remote_module_command(config, *, module_args, device_index=None):
    python = shlex.quote(config["remote_python"])
    code_dir = shlex.quote(config["remote_code_dir"])
    run_dir = shlex.quote(config["remote_run_dir"])
    prefix = f"PYTHONPATH={code_dir} OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 CUBLAS_WORKSPACE_CONFIG=:4096:8"
    if device_index is not None:
        prefix += f" CUDA_VISIBLE_DEVICES={int(device_index)}"
    return f"{prefix} {python} -m native_runner.training.sspi.value_curve {module_args} --run-dir {run_dir}"


def _prepare_remote(run_dir, config, *, host, python):
    remote_code = config["remote_code_dir"]
    remote_run = config["remote_run_dir"]
    _ssh(host, f"mkdir -p {shlex.quote(remote_code)} {shlex.quote(remote_run + '/checkpoints')} "
        f"{shlex.quote(remote_run + '/checks')} {shlex.quote(remote_run + '/derived')}")
    source_root = Path(__file__).resolve().parents[3]
    code_members = ("native_runner", "research/puct-runtime")
    source_checks = (
        "native_runner/training/sspi/value_curve.py",
        "research/puct-runtime/native_adapter.py",
        "research/puct-runtime/tree_search.py",
        "research/puct-runtime/search_profile.py")
    missing = False
    for relative in source_checks:
        local_file = source_root / relative
        remote_file = remote_code + "/" + relative
        remote_sha = _ssh(host, f"if test -f {shlex.quote(remote_file)}; then sha256sum {shlex.quote(remote_file)} | cut -d' ' -f1; fi")
        if remote_sha and remote_sha != _file_sha(local_file):
            raise ValueError(f"remote run source differs: {relative}")
        missing |= not remote_sha
    if missing:
        _sync_tar_to_remote(host, source_root, code_members, remote_code)
    remote_config = remote_run + "/run_config.json"
    config_sha = _ssh(host, f"if test -f {shlex.quote(remote_config)}; then sha256sum {shlex.quote(remote_config)} | cut -d' ' -f1; fi")
    local_config_sha = _file_sha(Path(run_dir) / "run_config.json")
    if config_sha and config_sha != local_config_sha:
        raise ValueError("remote run configuration differs from the local run identity")
    if not config_sha:
        _scp_to_remote(host, Path(run_dir) / "run_config.json", remote_config)
    initial_path = Path(run_dir) / "checkpoints" / "initial.pt"
    initial_sha = _file_sha(initial_path)
    remote_initial = remote_run + "/checkpoints/initial.pt"
    remote_initial_sha = _ssh(host, f"if test -f {shlex.quote(remote_initial)}; then sha256sum {shlex.quote(remote_initial)} | cut -d' ' -f1; fi")
    if not remote_initial_sha:
        _scp_to_remote(host, initial_path, remote_initial)
    elif remote_initial_sha != initial_sha:
        raise ValueError("remote initial checkpoint differs from local initialization")
    local_checks = []
    for record_path in sorted((Path(run_dir) / "checks").glob("check-*.json")):
        record = json.loads(record_path.read_text())
        end = record.get("training_games")
        checkpoint_path = Path(run_dir) / "checkpoints" / f"curve-{end:04d}.pt"
        if (record.get("schema") != CHECK_SCHEMA or not checkpoint_path.is_file()
                or _file_sha(checkpoint_path) != record.get("checkpoint", {}).get("file_sha256")):
            raise ValueError("local curve checkpoint or record checksum differs during recovery")
        local_checks.append((end, record_path, checkpoint_path))
        for local_path, remote_path in ((record_path, remote_run + f"/checks/{record_path.name}"),
                (checkpoint_path, remote_run + f"/checkpoints/{checkpoint_path.name}")):
            remote_sha = _ssh(host, f"if test -f {shlex.quote(remote_path)}; then sha256sum {shlex.quote(remote_path)} | cut -d' ' -f1; fi")
            local_sha = _file_sha(local_path)
            if not remote_sha:
                _scp_to_remote(host, local_path, remote_path)
            elif remote_sha != local_sha:
                raise ValueError("remote and local value-curve checkpoint evidence differs")
    remote_current = remote_run + "/checkpoints/current.pt"
    remote_current_sha = _ssh(host, f"if test -f {shlex.quote(remote_current)}; then sha256sum {shlex.quote(remote_current)} | cut -d' ' -f1; fi")
    if not remote_current_sha:
        latest_path = local_checks[-1][2] if local_checks else initial_path
        _scp_to_remote(host, latest_path, remote_current)
    config["remote_python"] = python
    module = _remote_module_command(config, module_args="remote-preflight", device_index=None)
    output = _ssh(host, module)
    preflight = json.loads(output.splitlines()[-1])
    if preflight.get("status") != "ready" or preflight.get("value_head_parameters") != VALUE_HEAD_PARAMETERS:
        raise RuntimeError("remote CUDA value-curve preflight failed")
    config["remote_cuda_preflight"] = preflight
    config["remote_device_index"] = preflight["selected_physical_device"]
    _atomic_json(Path(run_dir) / "run_config.json", config)
    _scp_to_remote(host, Path(run_dir) / "run_config.json", remote_config)
    return preflight


def _native_preflight(actual_port, belief_port, belief_owner_1, transport):
    from .collector import EventBatchedSSPINative, SearchNative, _make_environment
    native = (EventBatchedSSPINative("127.0.0.1", actual_port, timeout=90.,
        event_deltas=transport == "event-json-delta")
        if transport in ("event-json", "event-json-delta") else
        SearchNative("127.0.0.1", actual_port, transport=transport, timeout=90.))
    try:
        environment = _make_environment(native, production_semantic_bundle())
        return preflight_native_endpoints("127.0.0.1", actual_port,
            {0: belief_port, 1: belief_owner_1}, transport=transport, actual_env_id=0,
            ruleset_manifest=environment.ruleset_manifest, include_belief=False)
    finally:
        native.close_transport()


def _initialize_run(run_dir, *, run_id, actual_port, belief_port, belief_owner_1,
        transport, remote_host, remote_python, remote_base_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / "run_config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        expected = dict(schema=RUN_SCHEMA, run_id=run_id, seed=SEED,
            check_every_games=CHECK_EVERY_GAMES, max_train_games=MAX_TRAIN_GAMES,
            validation_games=VALIDATION_GAMES, holdout_games=HOLDOUT_GAMES,
            value_sample_probability=VALUE_SAMPLE_PROBABILITY,
            source_sha256=_source_identity(), transport=transport,
            native_ports=dict(actual=actual_port, belief_owner_0=belief_port,
                belief_owner_1=belief_owner_1), remote_host=remote_host,
            remote_python=remote_python,
            remote_base_dir=str(PurePosixPath(remote_base_dir) / run_id))
        if any(config.get(key) != value for key, value in expected.items()):
            raise ValueError("existing value-curve run configuration differs")
        return config
    bundle = production_semantic_bundle()
    model = initialize_fresh_model(SEED, device="cpu", bundle=bundle).eval()
    if _check_head_scope(model) != VALUE_HEAD_PARAMETERS:
        raise AssertionError("unexpected v4 value-head parameter count")
    optimizer = create_value_optimizer(model, head_only=True)
    if policy_head_sha(model) != semantic_digest(model.policy_head.state_dict()):
        raise AssertionError("policy head SHA is unstable")
    attestation = _native_preflight(actual_port, belief_port, belief_owner_1, transport)
    source_sha = _source_identity()
    checkpoint_identity = dict(protocol=RUN_SCHEMA, run_id=run_id, seed=SEED,
        model_schema=model.schema, architecture_sha=model.architecture_sha,
        source_sha256=source_sha, collector_source_sha=_source_sha(),
        engine_sha256=attestation["actual"]["libg_sha256"],
        initial_model_sha=_model_sha(model),
        initial_policy_function_sha=policy_function_sha(model),
        initial_policy_head_sha=policy_head_sha(model),
        value_order=["WIN", "LOSS"], training_scope="value_head_only")
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_path = checkpoint_dir / "initial.pt"
    if checkpoint_path.exists():
        raise RuntimeError("orphan initial value-curve checkpoint needs inspection")
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer,
        identity=checkpoint_identity, optimizer_steps=0,
        run_state=dict(training_games=0, training_rows=0, training_label_counts=[0, 0],
            optimizer_steps=0, trained_game_ranges=[], last_check_index=0))
    ports = dict(actual=actual_port, belief_owner_0=belief_port,
        belief_owner_1=belief_owner_1)
    remote_root = PurePosixPath(remote_base_dir) / run_id
    config = dict(schema=RUN_SCHEMA, run_id=run_id, seed=SEED,
        model_schema=model.schema, architecture_sha=model.architecture_sha,
        source_sha256=source_sha, collector_source_sha=_source_sha(),
        engine_sha256=attestation["actual"]["libg_sha256"],
        native_attestation=attestation, native_ports=ports, transport=transport,
        actor_device="cpu", training_device="remote-cuda",
        value_sample_probability=VALUE_SAMPLE_PROBABILITY, search_probability=0.,
        check_every_games=CHECK_EVERY_GAMES, max_train_games=MAX_TRAIN_GAMES,
        max_checks=MAX_TRAIN_GAMES // CHECK_EVERY_GAMES,
        validation_games=VALIDATION_GAMES, holdout_games=HOLDOUT_GAMES,
        seed_domains=dict(SEED_DOMAINS), training=dict(batch_size=TRAIN_BATCH_SIZE,
            microbatch_size=TRAIN_MICROBATCH_SIZE, optimizer="AdamW", lr=LEARNING_RATE,
            weight_decay=WEIGHT_DECAY, gradient_clip=GRADIENT_CLIP,
            value_head_parameters=VALUE_HEAD_PARAMETERS, state_exposures=1),
        initial_model_sha=_model_sha(model), initial_policy_function_sha=policy_function_sha(model),
        initial_policy_head_sha=policy_head_sha(model), checkpoint_identity=checkpoint_identity,
        initial_checkpoint=checkpoint,
        remote_host=remote_host, remote_python=remote_python,
        remote_base_dir=str(remote_root), remote_code_dir=str(remote_root / "code"),
        remote_run_dir=str(remote_root / "run"),
        repeated_test_correction=dict(method="Bonferroni paired complete-game bootstrap",
            familywise_alpha=FAMILYWISE_ALPHA,
            comparisons=(MAX_TRAIN_GAMES // CHECK_EVERY_GAMES) * 2,
            bootstrap_replicates=BOOTSTRAP_REPLICATES))
    _atomic_json(config_path, config)
    _atomic_json(run_dir / "status.json", dict(status="initialized", stage="ready_to_collect",
        completed_validation_games=0, completed_training_games=0, max_training_games=MAX_TRAIN_GAMES))
    return config


def _completed_checks(run_dir, config):
    records = []
    for path in sorted((Path(run_dir) / "checks").glob("check-*.json")):
        record = json.loads(path.read_text())
        end = (len(records) + 1) * CHECK_EVERY_GAMES
        checkpoint = Path(run_dir) / "checkpoints" / f"curve-{end:04d}.pt"
        if (record.get("schema") != CHECK_SCHEMA or record.get("run_id") != config["run_id"]
                or record.get("training_games") != end or end > MAX_TRAIN_GAMES
                or record.get("status") not in ("signal", "continue")
                or records and records[-1]["status"] == "signal"
                or not checkpoint.is_file()
                or record["checkpoint"]["file_sha256"] != _file_sha(checkpoint)):
            raise ValueError("local committed checks are missing, corrupt, or out of order")
        payload = _read_checkpoint_payload(checkpoint, config["checkpoint_identity"])
        if (payload["run_state"].get("training_games") != end
                or payload["model_sha"] != record["checkpoint"]["model_sha"]
                or payload["optimizer_steps"] != record["checkpoint"]["optimizer_steps"]):
            raise ValueError("local check and checkpoint exposure boundary differ")
        records.append(record)
    return records


def run_local(*, run_dir, run_id, actual_port=26789, belief_port=26790,
        belief_port_owner_1=26792, transport="event-json-delta", remote_host="gujiga@172.16.48.34",
        remote_python="/home/gujiga/firstlight_CR/.venv/bin/python",
        remote_base_dir="/dev/shm/nashura-sspi-value-curves"):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", run_id):
        raise ValueError("run id contains unsupported characters")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "orchestrator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = _initialize_run(run_dir, run_id=run_id, actual_port=actual_port,
            belief_port=belief_port, belief_owner_1=belief_port_owner_1,
            transport=transport, remote_host=remote_host, remote_python=remote_python,
            remote_base_dir=remote_base_dir)
        checkpoint_records = _completed_checks(run_dir, config)
        signal_seen = bool(checkpoint_records and checkpoint_records[-1]["status"] == "signal")
        holdout_path = run_dir / "final-holdout.json"
        if holdout_path.exists():
            holdout = json.loads(holdout_path.read_text())
            if (not checkpoint_records or holdout.get("schema") != HOLDOUT_SCHEMA
                    or holdout.get("run_id") != run_id
                    or holdout.get("checkpoint_sha256") != checkpoint_records[-1]["checkpoint"]["file_sha256"]):
                raise ValueError("completed holdout differs from the committed learning curve")
            return dict(status="complete", run_id=run_id,
                training_games=checkpoint_records[-1]["training_games"],
                validation_signal=signal_seen, checks=checkpoint_records, holdout=holdout)
        ports = config["native_ports"]
        actor = _new_model(device="cpu").eval()
        if _model_sha(actor) != config["initial_model_sha"]:
            payload = _read_checkpoint_payload(run_dir / "checkpoints" / "initial.pt",
                config["checkpoint_identity"])
            actor.load_state_dict(payload["model"], strict=True)
            if _model_sha(actor) != config["initial_model_sha"]:
                raise ValueError("restored frozen value-curve actor differs from initialization")
        for parameter in actor.parameters():
            parameter.requires_grad_(False)
        remote_preflight = _prepare_remote(run_dir, config, host=remote_host,
            python=remote_python)
        _log_line(run_dir / "runtime.log", dict(event="remote_cuda_preflight", **remote_preflight))
        validation_ids = [_game_id("validation", index)
            for index in range(1, VALIDATION_GAMES + 1)]
        for index in range(1, VALIDATION_GAMES + 1):
            _collect_one(run_dir, config, actor, "validation", index,
                ports=ports, transport=transport)
            _atomic_json(run_dir / "status.json", dict(status="running",
                stage="validation_collection", completed_validation_games=index,
                target_validation_games=VALIDATION_GAMES,
                completed_training_games=0, max_training_games=MAX_TRAIN_GAMES))
        _sync_episode_dirs(remote_host, run_dir, config["remote_run_dir"], validation_ids)
        start_time = time.monotonic()
        next_game = checkpoint_records[-1]["training_games"] + 1 if checkpoint_records else 1
        for batch_start in (() if signal_seen else range(next_game, MAX_TRAIN_GAMES + 1, CHECK_EVERY_GAMES)):
            batch_end = batch_start + CHECK_EVERY_GAMES - 1
            train_ids = []
            for index in range(batch_start, batch_end + 1):
                _collect_one(run_dir, config, actor, "training", index,
                    ports=ports, transport=transport)
                train_ids.append(_game_id("training", index))
                _atomic_json(run_dir / "status.json", dict(status="running",
                    stage="training_collection", completed_validation_games=VALIDATION_GAMES,
                    completed_training_games=index, target_training_games=batch_end,
                    max_training_games=MAX_TRAIN_GAMES, latest_game=train_ids[-1],
                    elapsed_seconds=time.monotonic() - start_time))
            _sync_training_batch(remote_host, run_dir, config["remote_run_dir"],
                batch_start, batch_end)
            module = _remote_module_command(config,
                module_args=f"train-batch --batch-start {batch_start} --batch-end {batch_end}",
                device_index=config["remote_device_index"])
            output = _ssh(remote_host, module)
            record = json.loads(output.splitlines()[-1])
            expected_record = run_dir / "checks" / f"check-{batch_end:04d}.json"
            _atomic_json(expected_record, record)
            checkpoint_path = config["remote_run_dir"] + f"/checkpoints/curve-{batch_end:04d}.pt"
            local_checkpoint = run_dir / "checkpoints" / f"curve-{batch_end:04d}.pt"
            _scp_from_remote(remote_host, checkpoint_path, local_checkpoint)
            if _file_sha(local_checkpoint) != record["checkpoint"]["file_sha256"]:
                raise ValueError("downloaded CUDA checkpoint checksum differs")
            _scp_from_remote(remote_host, config["remote_run_dir"] + "/checkpoints/current.pt",
                run_dir / "checkpoints" / "current.pt")
            checkpoint_records.append(record)
            _atomic_json(run_dir / "status.json", dict(status="running",
                stage="checkpoint_complete", completed_validation_games=VALIDATION_GAMES,
                completed_training_games=batch_end, max_training_games=MAX_TRAIN_GAMES,
                latest_check=record))
            _log_line(run_dir / "runtime.log", dict(event="checkpoint", record=record))
            if record["status"] == "signal":
                signal_seen = True
                break
        latest_games = checkpoint_records[-1]["training_games"]
        for index in range(1, HOLDOUT_GAMES + 1):
            _collect_one(run_dir, config, actor, "holdout", index,
                ports=ports, transport=transport)
            _atomic_json(run_dir / "status.json", dict(status="running",
                stage="holdout_collection", completed_validation_games=VALIDATION_GAMES,
                completed_training_games=latest_games,
                completed_holdout_games=index, target_holdout_games=HOLDOUT_GAMES,
                validation_signal=signal_seen))
        holdout_ids = [_game_id("holdout", index) for index in range(1, HOLDOUT_GAMES + 1)]
        _sync_episode_dirs(remote_host, run_dir, config["remote_run_dir"], holdout_ids)
        module = _remote_module_command(config, module_args="evaluate-holdout",
            device_index=config["remote_device_index"])
        output = _ssh(remote_host, module)
        holdout = json.loads(output.splitlines()[-1])
        _atomic_json(run_dir / "final-holdout.json", holdout)
        _atomic_json(run_dir / "status.json", dict(status="complete", stage="finished",
            training_games=latest_games, validation_signal=signal_seen, holdout=holdout,
            elapsed_seconds=time.monotonic() - start_time))
        return dict(status="complete", run_id=run_id, training_games=latest_games,
            validation_signal=signal_seen, checks=checkpoint_records, holdout=holdout)


def remote_preflight():
    result = _remote_preflight()
    print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
    return result


def _load_remote_config(run_dir):
    config = json.loads((Path(run_dir) / "run_config.json").read_text())
    if (config.get("schema") != RUN_SCHEMA or config.get("source_sha256") != _source_identity()
            or config.get("max_train_games") != MAX_TRAIN_GAMES
            or config.get("check_every_games") != CHECK_EVERY_GAMES):
        raise ValueError("remote value-curve code or run configuration differs")
    return config


def train_batch_cli(run_dir, batch_start, batch_end, device):
    if torch.device(device).type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("this experiment requires remote CUDA training")
    with (Path(run_dir) / "update.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = _write_remote_training_batch(run_dir,
            _load_remote_config(run_dir), batch_start, batch_end, device=device)
    print(json.dumps(record, sort_keys=True, allow_nan=False), flush=True)
    return record


def evaluate_holdout_cli(run_dir, device):
    config = _load_remote_config(run_dir)
    record = _remote_holdout(run_dir, config, device=device)
    print(json.dumps(record, sort_keys=True, allow_nan=False), flush=True)
    return record


def main():
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="collect, update, and evaluate the learning curve")
    run_parser.add_argument("--run-dir", type=Path, required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--actual-port", type=int, default=26789)
    run_parser.add_argument("--belief-port", type=int, default=26790)
    run_parser.add_argument("--belief-port-owner-1", type=int, default=26792)
    run_parser.add_argument("--transport", default="event-json-delta",
        choices=("legacy", "persistent", "event-json", "event-json-delta"))
    run_parser.add_argument("--remote-host", default="gujiga@172.16.48.34")
    run_parser.add_argument("--remote-python",
        default="/home/gujiga/firstlight_CR/.venv/bin/python")
    run_parser.add_argument("--remote-base-dir",
        default="/dev/shm/nashura-sspi-value-curves")
    remote_parser = subparsers.add_parser("remote-preflight")
    remote_parser.add_argument("--run-dir", type=Path, required=True)
    train_parser = subparsers.add_parser("train-batch")
    train_parser.add_argument("--run-dir", type=Path, required=True)
    train_parser.add_argument("--batch-start", type=int, required=True)
    train_parser.add_argument("--batch-end", type=int, required=True)
    train_parser.add_argument("--device", default="cuda:0")
    holdout_parser = subparsers.add_parser("evaluate-holdout")
    holdout_parser.add_argument("--run-dir", type=Path, required=True)
    holdout_parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    try:
        if args.command == "run":
            result = run_local(run_dir=args.run_dir, run_id=args.run_id,
                actual_port=args.actual_port, belief_port=args.belief_port,
                belief_port_owner_1=args.belief_port_owner_1, transport=args.transport,
                remote_host=args.remote_host, remote_python=args.remote_python,
                remote_base_dir=args.remote_base_dir)
        elif args.command == "remote-preflight":
            result = remote_preflight()
        elif args.command == "train-batch":
            result = train_batch_cli(args.run_dir, args.batch_start, args.batch_end, args.device)
        else:
            result = evaluate_holdout_cli(args.run_dir, args.device)
    except Exception as error:
        if args.command == "run":
            failure = dict(status="failed", exception=type(error).__name__,
                reason=str(error), traceback=traceback.format_exc())
            _log_line(args.run_dir / "failures.jsonl", failure)
            _atomic_json(args.run_dir / "status.json", failure)
        raise
    if args.command == "run":
        print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
