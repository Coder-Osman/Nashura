"""Eight-generation, no-search Hog outcome-learning experiment."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import traceback

import torch

from ..fair_search.contracts import semantic_digest
from ..v4.factory import production_semantic_bundle
from .collector import VALUE_ARM, _source_sha, collect_episode, episode_seed_identity
from .experiment import paired_win_rate_interval, paired_outcomes_from_results
from .learning import load_checkpoint, save_checkpoint, decisive_result
from .model import initialize_fresh_model
from .value_learning import (create_value_optimizer, load_value_episode,
    policy_head_sha, prediction_metrics, train_value_generation, value_predictions)


RUN_SCHEMA = "sspi-value-only-run.v2"
GENERATION_SCHEMA = "sspi-value-only-generation.v2"
SEED = 20261103
GENERATIONS = 8
TRAIN_GAMES = 64
VALIDATION_GAMES = 16
FINAL_HOLDOUT_GAMES = 16
EVALUATION_PAIRS = 1500


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _source_identity():
    return semantic_digest(dict(collector=_source_sha(),
        value_learning=hashlib.sha256(Path(__file__).with_name("value_learning.py").read_bytes()).hexdigest(),
        value_experiment=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))


def _model_sha(model):
    return semantic_digest(model.state_dict())


def _checkpoint_model(path, identity, *, bundle):
    model = initialize_fresh_model(SEED, device="cpu", bundle=bundle)
    optimizer = create_value_optimizer(model)
    restoration = load_checkpoint(path, model, optimizer,
        expected_identity=identity, device="cpu")
    if restoration["model_sha"] != _model_sha(model):
        raise ValueError("value-only checkpoint failed model restoration")
    return model, optimizer, restoration


def _collect(run_dir, config, game_id, kind, model, *, actual_port, belief_port,
        belief_port_owner_1, transport, random_stream_id=None):
    destination = run_dir / "episodes" / game_id
    models = model if isinstance(model, dict) else {0: model, 1: model}
    expected_shas = {str(owner): _model_sha(item) for owner, item in models.items()}
    if destination.exists():
        if (destination / "quarantine.json").exists() or not (destination / "episode.json").is_file():
            raise RuntimeError(f"existing value-only episode needs inspection: {destination}")
        metadata = json.loads((destination / "episode.json").read_text())
    else:
        metadata = collect_episode(actual_port=actual_port, belief_port=belief_port,
            belief_ports={0: belief_port, 1: belief_port_owner_1},
            output_dir=destination, run_id=config["run_id"], arm=VALUE_ARM,
            game_id=game_id, seed=SEED, models_by_owner=models, device="cpu",
            transport=transport, episode_kind=kind, search_probability=0.,
            temporal_augmentation=False, random_stream_id=random_stream_id,
            value_sample_probability=0. if kind == "evaluation" else 0.5)
    identity = metadata.get("identity") or {}
    decisive = decisive_result(metadata.get("result"))
    if (metadata.get("status") != "complete"
            or metadata.get("schema") != "sspi-value-only-episode.v2"
            or metadata.get("search_roots") != 0
            or metadata.get("search_probability") != 0.
            or metadata.get("episode_kind") != kind
            or metadata.get("training_admissible") is not (kind == "training" and decisive)
            or metadata.get("value_sample_probability") != (0. if kind == "evaluation" else 0.5)
            or identity.get("collection_protocol") != "value-only.v2"
            or identity.get("value_sample_probability") != metadata["value_sample_probability"]
            or identity.get("run_id") != config["run_id"]
            or identity.get("game_id") != game_id
            or identity.get("model_sha256_by_owner") != expected_shas
            or identity.get("source_sha256") != config["collector_source_sha"]
            or identity.get("native_transport") != transport
            or identity.get("engine_sha256") != config["engine_sha256"]
            or identity.get("random_stream_id") != (random_stream_id or game_id)):
        raise ValueError("value-only native episode differs from the frozen run identity")
    for filename, checksum in (("episode.pt", "tensor_sha256"),
            ("actions.json", "actions_sha256")):
        if hashlib.sha256((destination / filename).read_bytes()).hexdigest() != metadata.get(checksum):
            raise ValueError("value-only native episode file checksum differs")
    return destination, metadata


def _collect_rows(run_dir, config, prefix, count, kind, model, *, actual_port,
        belief_port, belief_port_owner_1, transport):
    rows, episode_shas, elapsed = [], {}, []
    outcomes = dict(collected_games=0, decisive_games=0, excluded_draw_games=0)
    model_sha = _model_sha(model)
    for index in range(1, count + 1):
        game_id = f"{prefix}-{index:04d}"
        directory, metadata = _collect(run_dir, config, game_id, kind, model,
            actual_port=actual_port, belief_port=belief_port,
            belief_port_owner_1=belief_port_owner_1, transport=transport)
        selected, _ = load_value_episode(directory, expected_kind=kind,
            run_id=config["run_id"], model_sha=model_sha)
        rows.extend(selected)
        episode_shas[game_id] = hashlib.sha256((directory / "episode.json").read_bytes()).hexdigest()
        elapsed.append(metadata["elapsed_seconds"])
        decisive = decisive_result(metadata["result"])
        outcomes["collected_games"] += 1
        outcomes["decisive_games" if decisive else "excluded_draw_games"] += 1
        _atomic_json(run_dir / "status.json", dict(status="running", stage=f"{prefix}_collection",
            completed=index, target=count, selected_rows=len(rows), latest_game=game_id,
            latest_wall_seconds=elapsed[-1], outcomes=outcomes))
    return rows, episode_shas, elapsed, outcomes


def _constant_predictions(training_rows, validation_rows):
    if not training_rows:
        raise ValueError("constant baseline requires decisive training samples")
    counts = [1., 1.]
    for row in training_rows:
        counts[int(row.final_wl)] += 1.
    total = sum(counts)
    probabilities = tuple(count / total for count in counts)
    return [(row.game_id, int(row.final_wl), probabilities) for row in validation_rows]


def _bootstrap_ce_difference(first, second, *, seed, replicates=2000):
    if (len(first) != len(second) or not first
            or any(a[:2] != b[:2] for a, b in zip(first, second))):
        raise ValueError("paired value predictions require identical held-out decisions")
    games = sorted({item[0] for item in first})
    by_game = {}
    for a, b in zip(first, second):
        difference = math.log(max(b[2][b[1]], 1e-12)) - math.log(max(a[2][a[1]], 1e-12))
        total, count = by_game.get(a[0], (0., 0))
        by_game[a[0]] = (total + difference, count + 1)
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        drawn = [by_game[games[rng.randrange(len(games))]] for _ in games]
        samples.append(sum(item[0] for item in drawn) / sum(item[1] for item in drawn))
    samples.sort()
    return dict(mean=sum(item[0] for item in by_game.values()) / len(first),
        low=samples[int(.025 * (replicates - 1))],
        high=samples[int(.975 * (replicates - 1))], games=len(games),
        method="game-bootstrap-ce-difference", seed=seed)


def _policy_drift(initial, model, probe):
    waits, initial_waits, kls = [], [], []
    initial.eval()
    model.eval()
    with torch.inference_mode():
        for row in probe:
            before = initial(row.information, [row.information.legality])[0][0].conditionals().exp()
            after = model(row.information, [row.information.legality])[0][0].conditionals().exp()
            initial_waits.append(float(before[0]))
            waits.append(float(after[0]))
            kls.append(float((before * (before.clamp_min(1e-12).log()
                - after.clamp_min(1e-12).log())).sum()))
    return dict(probe_rows=len(probe), initial_wait=sum(initial_waits) / len(probe),
        current_wait=sum(waits) / len(probe), mode_kl_from_initial=sum(kls) / len(probe))


def _run_benchmark(run_dir, config, model, *, actual_port, belief_port,
        belief_port_owner_1, transport):
    records = []
    for index in range(1, 4):
        game_id = f"benchmark-{index:04d}"
        directory, metadata = _collect(run_dir, config, game_id, "validation", model,
            actual_port=actual_port, belief_port=belief_port,
            belief_port_owner_1=belief_port_owner_1, transport=transport)
        records.append(dict(game_id=game_id, wall_seconds=metadata["elapsed_seconds"],
            decisions=metadata["decisions"]["decisions"],
            value_samples=metadata["value_samples"],
            tensor_bytes=(directory / "episode.pt").stat().st_size,
            timing_profile=metadata["timing_profile"],
            native_advance_performance=metadata["native_advance_performance"]))
    _atomic_json(run_dir / "benchmark.json", dict(schema="sspi-value-only-benchmark.v1",
        run_id=config["run_id"], games=records))


def _run_generation(run_dir, config, generation, *, bundle, actual_port, belief_port,
        belief_port_owner_1, transport):
    parent_path = run_dir / "checkpoints" / f"generation-{generation - 1:04d}.pt"
    parent_record = (config if generation == 1 else
        json.loads((run_dir / f"generation-{generation - 1:04d}.json").read_text()))
    parent_identity = parent_record["checkpoint_identity"]
    model, optimizer, parent_restore = _checkpoint_model(parent_path, parent_identity, bundle=bundle)
    parent_sha = parent_restore["model_sha"]
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model.eval()
    prefix = f"train-g{generation:04d}"
    training_rows, train_shas, train_times, train_outcomes = _collect_rows(run_dir, config,
        prefix, TRAIN_GAMES, "training", model, actual_port=actual_port,
        belief_port=belief_port, belief_port_owner_1=belief_port_owner_1,
        transport=transport)
    validation_rows, validation_shas, validation_times, validation_outcomes = _collect_rows(run_dir, config,
        f"validation-g{generation:04d}", VALIDATION_GAMES, "validation", model,
        actual_port=actual_port, belief_port=belief_port,
        belief_port_owner_1=belief_port_owner_1, transport=transport)
    if not training_rows or not validation_rows:
        record = dict(schema=GENERATION_SCHEMA, run_id=config["run_id"],
            status="skipped", generation=generation,
            reason="no_effective_training_or_validation_samples", optimizer_steps=0,
            training_outcomes=train_outcomes, validation_outcomes=validation_outcomes,
            train_episode_sha256=train_shas, validation_episode_sha256=validation_shas,
            parent_model_sha=parent_sha, parent_checkpoint_sha256=parent_restore["file_sha"])
        _atomic_json(run_dir / f"generation-{generation:04d}.json", record)
        _atomic_json(run_dir / "status.json", dict(record, stage="generation_update_skipped"))
        return record
    initial = initialize_fresh_model(SEED, device="cpu", bundle=bundle).eval()
    initial_predictions = value_predictions(initial, validation_rows)
    constant_predictions = _constant_predictions(training_rows, validation_rows)
    update = train_value_generation(model, optimizer, training_rows, validation_rows,
        seed=SEED ^ generation ^ 0x56414c55)
    current_predictions = value_predictions(model, validation_rows)
    if generation == 1:
        probe = validation_rows[:32]
    else:
        probe = []
        for index in range(1, VALIDATION_GAMES + 1):
            directory = run_dir / "episodes" / f"validation-g0001-{index:04d}"
            source, _ = load_value_episode(directory, expected_kind="validation",
                run_id=config["run_id"], model_sha=config["initial_model_sha"])
            probe.extend(source)
            if len(probe) >= 32:
                break
        probe = probe[:32]
    checkpoint_identity = dict(protocol=RUN_SCHEMA, run_id=config["run_id"],
        generation=generation, seed=SEED, architecture_sha=model.architecture_sha,
        source_sha256=config["source_sha256"], engine_sha256=config["engine_sha256"],
        parent_checkpoint_sha256=parent_restore["file_sha"], parent_model_sha256=parent_sha,
        train_episode_sha256=train_shas, validation_episode_sha256=validation_shas,
        policy_head_sha=policy_head_sha(model), training_seed=update["seed"])
    checkpoint_path = run_dir / "checkpoints" / f"generation-{generation:04d}.pt"
    if checkpoint_path.exists():
        raise RuntimeError("orphan value-only checkpoint needs inspection before replay")
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer, identity=checkpoint_identity,
        optimizer_steps=parent_restore["optimizer_steps"] + update["optimizer_steps"],
        run_state=dict(generation=generation, train_games=TRAIN_GAMES,
            validation_games=VALIDATION_GAMES, value_rows=len(training_rows),
            validation_rows=len(validation_rows), attempted_steps=update["attempted_steps"],
            retained_steps=update["optimizer_steps"]))
    restored, restored_optimizer, restored_info = _checkpoint_model(
        checkpoint_path, checkpoint_identity, bundle=bundle)
    if (restored_info["file_sha"] != checkpoint["file_sha"]
            or policy_head_sha(restored) != config["initial_policy_head_sha"]
            or not restored_optimizer.state):
        raise ValueError("value-only checkpoint or frozen policy head failed restoration")
    record = dict(schema=GENERATION_SCHEMA, run_id=config["run_id"], status="updated",
        generation=generation, checkpoint=checkpoint, checkpoint_identity=checkpoint_identity,
        parent_model_sha=parent_sha, train_games=TRAIN_GAMES, validation_games=VALIDATION_GAMES,
        value_samples=len(training_rows), validation_samples=len(validation_rows),
        training_outcomes=train_outcomes, validation_outcomes=validation_outcomes,
        train_episode_sha256=train_shas, validation_episode_sha256=validation_shas,
        mean_train_game_seconds=sum(train_times) / len(train_times),
        mean_validation_game_seconds=sum(validation_times) / len(validation_times),
        update=update, value_metrics=dict(current=prediction_metrics(current_predictions),
            theta0=prediction_metrics(initial_predictions),
            constant=prediction_metrics(constant_predictions),
            current_minus_theta0=_bootstrap_ce_difference(current_predictions,
                initial_predictions, seed=SEED ^ generation),
            current_minus_constant=_bootstrap_ce_difference(current_predictions,
                constant_predictions, seed=SEED ^ generation ^ 0x434f4e53)),
        policy_drift=_policy_drift(initial, restored, probe))
    _atomic_json(run_dir / f"generation-{generation:04d}.json", record)
    _atomic_json(run_dir / "status.json", dict(status="complete", stage="generation_complete",
        generation=generation, checkpoint_sha256=checkpoint["file_sha"],
        validation_ce=record["value_metrics"]["current"]["wl_ce"]))
    return record


def _final_holdout(run_dir, config, model, *, bundle, actual_port, belief_port,
        belief_port_owner_1, transport):
    rows, shas, times, outcomes = _collect_rows(run_dir, config, "holdout-final",
        FINAL_HOLDOUT_GAMES, "validation", model, actual_port=actual_port,
        belief_port=belief_port, belief_port_owner_1=belief_port_owner_1,
        transport=transport)
    initial = initialize_fresh_model(SEED, device="cpu", bundle=bundle).eval()
    training_rows = []
    final_record = json.loads((run_dir / "generation-0008.json").read_text())
    for game_id in final_record["train_episode_sha256"]:
        training, _ = load_value_episode(run_dir / "episodes" / game_id,
            expected_kind="training", run_id=config["run_id"],
            model_sha=final_record["parent_model_sha"])
        training_rows.extend(training)
    current = value_predictions(model, rows)
    initial_predictions = value_predictions(initial, rows)
    constant = _constant_predictions(training_rows, rows)
    record = dict(schema="sspi-value-only-final-holdout.v2", run_id=config["run_id"],
        model_sha=_model_sha(model), episode_sha256=shas, games=FINAL_HOLDOUT_GAMES,
        selected_rows=len(rows), mean_game_seconds=sum(times) / len(times),
        outcomes=outcomes, status="defined" if rows else "undefined",
        current=prediction_metrics(current) if rows else None,
        theta0=prediction_metrics(initial_predictions) if rows else None,
        constant=prediction_metrics(constant) if rows else None,
        current_minus_constant=_bootstrap_ce_difference(current, constant, seed=SEED) if rows else None,
        current_minus_theta0=_bootstrap_ce_difference(current, initial_predictions,
            seed=SEED ^ 0x484f4c44) if rows else None)
    _atomic_json(run_dir / "final-holdout.json", record)
    return record


def _paired_evaluation(run_dir, config, model, *, bundle, actual_port, belief_port,
        belief_port_owner_1, transport):
    initial = initialize_fresh_model(SEED, device="cpu", bundle=bundle).eval()
    results = []
    for pair in range(1, EVALUATION_PAIRS + 1):
        stream = f"value-only-eval-pair-{pair:04d}"
        for side in (0, 1):
            game_id = f"paired-eval-{pair:04d}-side-{side}"
            models = {side: model, 1 - side: initial}
            _, metadata = _collect(run_dir, config, game_id, "evaluation", models,
                actual_port=actual_port, belief_port=belief_port,
                belief_port_owner_1=belief_port_owner_1, transport=transport,
                random_stream_id=stream)
            winner = metadata["result"]["winner"]
            decisive_result(metadata["result"])
            identity = metadata["identity"]
            expected_seeds = episode_seed_identity(SEED, run_id=config["run_id"],
                arm=VALUE_ARM, game_id=game_id, random_stream_id=stream)
            if any(identity.get(key) != value for key, value in expected_seeds.items()):
                raise ValueError("value-only paired native seed identity differs")
            score = None if winner is None else float(winner == side)
            results.append(dict(pair_index=pair, trained_owner=side,
                side=side, game_id=game_id, score=score,
                random_stream_id=stream, actual_match_seed=identity["actual_match_seed"],
                actual_rollout_seed=identity["actual_rollout_seed"],
                search_probability=0., search_roots=0,
                winner=winner, episode_sha256=hashlib.sha256(
                    (run_dir / "episodes" / game_id / "episode.json").read_bytes()).hexdigest()))
        _atomic_json(run_dir / "evaluation-progress.json", dict(run_id=config["run_id"],
            complete_pairs=pair, games=len(results)))
    pair_counts = paired_outcomes_from_results(results, expected_pairs=EVALUATION_PAIRS)
    record = dict(schema="sspi-value-only-paired-evaluation.v2",
        run_id=config["run_id"], model_sha=_model_sha(model),
        theta0_sha=config["initial_model_sha"], games=results,
        pair_counts=pair_counts,
        score=paired_win_rate_interval(pair_counts, seed=SEED ^ 0x4556414c))
    _atomic_json(run_dir / "evaluation-paired-final.json", record)
    return record


def run(*, run_dir, run_id, actual_port=26789, belief_port=26790,
        belief_port_owner_1=26792, transport="event-json-delta", generation_limit=8):
    if type(generation_limit) is not int or not 0 <= generation_limit <= GENERATIONS:
        raise ValueError("generation limit must be between zero and eight")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    bundle = production_semantic_bundle()
    source_sha = _source_identity()
    config_path = run_dir / "run_config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if (config.get("schema") != RUN_SCHEMA or config.get("run_id") != run_id
                or config.get("seed") != SEED or config.get("generations") != GENERATIONS
                or config.get("train_games") != TRAIN_GAMES
                or config.get("validation_games") != VALIDATION_GAMES
                or config.get("source_sha256") != source_sha
                or config.get("transport") != transport):
            raise ValueError("existing value-only run configuration differs")
    else:
        model = initialize_fresh_model(SEED, device="cpu", bundle=bundle).eval()
        optimizer = create_value_optimizer(model)
        from .collector import preflight_native_endpoints, _make_environment, EventBatchedSSPINative
        from .collector import SearchNative
        native = (EventBatchedSSPINative("127.0.0.1", actual_port, timeout=90.,
            event_deltas=transport == "event-json-delta")
            if transport in ("event-json", "event-json-delta") else
            SearchNative("127.0.0.1", actual_port, transport=transport))
        try:
            environment = _make_environment(native, bundle)
            attestation = preflight_native_endpoints("127.0.0.1", actual_port,
                {0: belief_port, 1: belief_port_owner_1}, transport=transport,
                actual_env_id=0, ruleset_manifest=environment.ruleset_manifest,
                include_belief=False)
        finally:
            native.close_transport()
        initial_identity = dict(protocol=RUN_SCHEMA, run_id=run_id, generation=0,
            seed=SEED, architecture_sha=model.architecture_sha,
            source_sha256=source_sha, engine_sha256=attestation["actual"]["libg_sha256"],
            policy_head_sha=policy_head_sha(model))
        if (run_dir / "checkpoints" / "generation-0000.pt").exists():
            raise RuntimeError("orphan value-only initialization checkpoint needs inspection")
        checkpoint = save_checkpoint(run_dir / "checkpoints" / "generation-0000.pt",
            model, optimizer, identity=initial_identity, optimizer_steps=0)
        config = dict(schema=RUN_SCHEMA, run_id=run_id, seed=SEED,
            generations=GENERATIONS, train_games=TRAIN_GAMES,
            validation_games=VALIDATION_GAMES, final_holdout_games=FINAL_HOLDOUT_GAMES,
            evaluation_pairs=EVALUATION_PAIRS, value_sample_probability=0.5,
            search_probability=0., transport=transport, source_sha256=source_sha,
            native_ports=dict(actual=actual_port, belief_owner_0=belief_port,
                belief_owner_1=belief_port_owner_1),
            collector_source_sha=_source_sha(),
            engine_sha256=attestation["actual"]["libg_sha256"],
            initial_model_sha=_model_sha(model),
            initial_policy_head_sha=policy_head_sha(model),
            checkpoint_identity=initial_identity, initial_checkpoint=checkpoint)
        _atomic_json(config_path, config)
    if config.get("native_ports") != dict(actual=actual_port,
            belief_owner_0=belief_port, belief_owner_1=belief_port_owner_1):
        raise ValueError("value-only native ports differ from the frozen run configuration")
    if hashlib.sha256((run_dir / "checkpoints" / "generation-0000.pt").read_bytes()).hexdigest() \
            != config["initial_checkpoint"]["file_sha"]:
        raise ValueError("value-only initialization checkpoint checksum differs")
    if not (run_dir / "benchmark.json").exists():
        initial, _, _ = _checkpoint_model(run_dir / "checkpoints" / "generation-0000.pt",
            config["checkpoint_identity"], bundle=bundle)
        _run_benchmark(run_dir, config, initial, actual_port=actual_port,
            belief_port=belief_port, belief_port_owner_1=belief_port_owner_1,
            transport=transport)
    for generation in range(1, generation_limit + 1):
        record_path = run_dir / f"generation-{generation:04d}.json"
        if record_path.exists():
            record = json.loads(record_path.read_text())
            if record.get("schema") != GENERATION_SCHEMA or record.get("run_id") != run_id:
                raise ValueError("existing value-only generation record differs")
            if record.get("status") == "skipped":
                return record
            _checkpoint_model(run_dir / "checkpoints" / f"generation-{generation:04d}.pt",
                record["checkpoint_identity"], bundle=bundle)
            continue
        record = _run_generation(run_dir, config, generation, bundle=bundle,
            actual_port=actual_port, belief_port=belief_port,
            belief_port_owner_1=belief_port_owner_1, transport=transport)
        if record.get("status") == "skipped":
            return record
    if generation_limit == GENERATIONS:
        final = json.loads((run_dir / "generation-0008.json").read_text())
        model, _, _ = _checkpoint_model(run_dir / "checkpoints" / "generation-0008.pt",
            final["checkpoint_identity"], bundle=bundle)
        model.eval()
        if not (run_dir / "final-holdout.json").exists():
            _final_holdout(run_dir, config, model, bundle=bundle, actual_port=actual_port,
                belief_port=belief_port, belief_port_owner_1=belief_port_owner_1,
                transport=transport)
        elif json.loads((run_dir / "final-holdout.json").read_text()).get("model_sha") != _model_sha(model):
            raise ValueError("existing final value holdout belongs to another model")
        if not (run_dir / "evaluation-paired-final.json").exists():
            _paired_evaluation(run_dir, config, model, bundle=bundle,
                actual_port=actual_port, belief_port=belief_port,
                belief_port_owner_1=belief_port_owner_1, transport=transport)
        elif json.loads((run_dir / "evaluation-paired-final.json").read_text()).get("model_sha") \
                != _model_sha(model):
            raise ValueError("existing paired evaluation belongs to another model")
        _atomic_json(run_dir / "status.json", dict(status="complete", stage="finished",
            generation=GENERATIONS, evaluated_games=2 * EVALUATION_PAIRS))


def main():
    parser = argparse.ArgumentParser(description="Run the no-search SSPI value experiment")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actual-port", type=int, default=26789)
    parser.add_argument("--belief-port", type=int, default=26790)
    parser.add_argument("--belief-port-owner-1", type=int, default=26792)
    parser.add_argument("--transport", default="event-json-delta",
        choices=("legacy", "persistent", "event-json", "event-json-delta"))
    parser.add_argument("--generation-limit", type=int, default=8)
    args = parser.parse_args()
    owns_run = False
    try:
        config_path = args.run_dir / "run_config.json"
        if config_path.exists():
            existing = json.loads(config_path.read_text())
            if (existing.get("schema") != RUN_SCHEMA or existing.get("run_id") != args.run_id
                    or existing.get("source_sha256") != _source_identity()):
                raise ValueError("existing run identity differs; preserve its evidence unchanged")
        args.run_dir.mkdir(parents=True, exist_ok=True)
        with (args.run_dir / "orchestrator.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owns_run = True
            run(run_dir=args.run_dir, run_id=args.run_id, actual_port=args.actual_port,
                belief_port=args.belief_port, belief_port_owner_1=args.belief_port_owner_1,
                transport=args.transport, generation_limit=args.generation_limit)
    except BaseException as error:
        if owns_run:
            _atomic_json(args.run_dir / "status.json", dict(status="failed",
                exception=type(error).__name__, reason=str(error),
                traceback=traceback.format_exc()))
        raise


if __name__ == "__main__":
    main()
