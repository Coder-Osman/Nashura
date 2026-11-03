"""Optional post-SSPI direct self-play evaluation of a fixed policy function."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path

import torch

from ..fair_search.contracts import semantic_digest
from .collector import VALUE_ARM, _source_sha, collect_episode
from .learning import decisive_result, load_checkpoint, save_checkpoint
from .model import VALUE_ORDER
from .value_learning import (create_value_optimizer, load_value_episode,
    policy_function_sha, prediction_metrics, train_value_generation, value_predictions)


STAGE_SCHEMA = "sspi-direct-value-stage.v1"


def stage_config(run_config):
    """An absent plan preserves historical SSPI execution exactly."""
    plan = run_config.get("direct_value_stage")
    if plan is None:
        return None
    if (not isinstance(plan, dict) or set(plan) != {"schema", "train_games", "validation_games"}
            or plan["schema"] != STAGE_SCHEMA
            or any(type(plan[key]) is not int or plan[key] < 1
                for key in ("train_games", "validation_games"))):
        raise ValueError("direct value stage requires explicit positive train/validation game counts")
    return dict(plan)


def _atomic_json(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_value_optimizer_state(model, source, destination):
    """Carry AdamW moments across phases without touching policy/shared moments."""
    for parameter in model.value_head.parameters():
        if parameter in source.state:
            destination.state[parameter] = copy.deepcopy(source.state[parameter])
        else:
            destination.state.pop(parameter, None)


def _collect_rows(directory, *, config, identity, model, kind, count,
        actual_port, belief_port, transport, progress):
    rows, files, walls = [], {}, []
    outcomes = dict(collected_games=0, decisive_games=0, excluded_draw_games=0)
    for index in range(1, count + 1):
        game_id = f"{kind}-{index:04d}"
        output = directory / "episodes" / game_id
        if output.exists():
            if (output / "quarantine.json").exists() or not (output / "episode.json").is_file():
                raise RuntimeError(f"direct value episode requires inspection: {output}")
        else:
            collect_episode(actual_port=actual_port, belief_port=belief_port,
                actual_env_id=config.get("actual_env_id", 0), output_dir=output,
                run_id=identity["stage_run_id"], arm=VALUE_ARM, game_id=game_id,
                seed=config["seed"], model=model, device="cpu", transport=transport,
                episode_kind=kind, search_probability=0., value_sample_probability=.5,
                temporal_augmentation=False)
        selected, metadata = load_value_episode(output, expected_kind=kind,
            run_id=identity["stage_run_id"], model_sha=identity["input_model_sha"])
        recorded = metadata["identity"]
        if (recorded.get("game_id") != game_id or recorded.get("random_stream_id") != game_id
                or recorded.get("seed") != config["seed"] or recorded.get("device") != "cpu"
                or recorded.get("source_sha256") != identity["collector_source_sha"]
                or recorded.get("engine_sha256") != identity["engine_sha"]
                or recorded.get("actual_env_id") != identity["actual_env_id"]
                or recorded.get("native_ports", {}).get("actual") != actual_port
                or recorded.get("native_transport") != transport):
            raise ValueError("direct value episode source, engine, seed or game identity differs")
        rows.extend(selected)
        for filename in ("episode.json", "episode.pt", "actions.json"):
            path = output / filename
            files[str(path.relative_to(directory))] = _sha(path)
        decisive = decisive_result(metadata["result"])
        outcomes["collected_games"] += 1
        outcomes["decisive_games" if decisive else "excluded_draw_games"] += 1
        walls.append(metadata["elapsed_seconds"])
        status = dict(status="running", stage=f"direct_value_{kind}",
            generation=identity["generation"], completed=index, target=count,
            selected_rows=len(rows), outcomes=dict(outcomes), owner_pid=os.getpid())
        _atomic_json(directory / "status.json", status)
        if progress is not None:
            progress(status)
    return rows, dict(files=files, outcomes=outcomes, wall_seconds=walls)


def _verify_files(directory, report):
    for kind in ("training", "validation"):
        for relative, expected in report[kind]["files"].items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or _sha(directory / path) != expected:
                raise ValueError("direct value episode evidence checksum differs")


def run_direct_value_stage(*, run_dir, config, generation, model, optimizer,
        optimizer_steps, engine_sha, actual_port, belief_port, transport, progress=None):
    """Append a head-only stage, preserving the original SSPI optimizer and protocol."""
    plan = stage_config(config)
    if plan is None:
        return None
    if (type(generation) is not int or generation < 1 or not isinstance(engine_sha, str)
            or not engine_sha or type(optimizer) is not torch.optim.AdamW
            or config.get("device") != "cpu"):
        raise ValueError("invalid direct value stage identity or optimizer")
    optimized = [p for group in optimizer.param_groups for p in group["params"]]
    if len(optimized) != len(list(model.parameters())) or {id(p) for p in optimized} != {
            id(p) for p in model.parameters()}:
        raise ValueError("direct value stage requires the full SSPI optimizer")
    directory = Path(run_dir) / "direct-value" / f"generation-{generation:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = dict(schema=STAGE_SCHEMA, plan=plan, run_id=config["run_id"],
            stage_run_id=f"{config['run_id']}:direct-value:g{generation:04d}",
            generation=generation, seed=config["seed"], architecture_sha=model.architecture_sha,
            value_order=list(VALUE_ORDER), input_model_sha=semantic_digest(model.state_dict()),
            input_optimizer_sha=semantic_digest(optimizer.state_dict()),
            input_optimizer_steps=optimizer_steps, policy_function_sha=policy_function_sha(model),
            collector_source_sha=_source_sha(), engine_sha=engine_sha, transport=transport,
            actual_env_id=config.get("actual_env_id", 0), actual_port=actual_port,
            belief_port=belief_port,
            stage_source_sha=_sha(Path(__file__)),
            learning_source_sha=_sha(Path(__file__).with_name("value_learning.py")),
            metrics_source_sha=_sha(Path(__file__).with_name("value_experiment.py")),
            trainable_scope="value_head", value_sample_probability=.5,
            optimizer=dict(type="AdamW", lr=1e-4, weight_decay=1e-4,
                state="continue-value-moments-from-sspi", batch=128, gradient_clip=1.))
        plan_path, checkpoint_path = directory / "plan.json", directory / "checkpoint.pt"
        if plan_path.exists():
            if json.loads(plan_path.read_text()) != identity:
                raise ValueError("direct value stage identity changed; use a new run")
        else:
            _atomic_json(plan_path, identity)
        flags = {name: p.requires_grad for name, p in model.named_parameters()}
        try:
            for parameter in model.parameters():
                parameter.requires_grad_(False)
            model.eval()
            if checkpoint_path.exists():
                # A durable checkpoint is also the commit record after an interrupted JSON write.
                payload = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
                report = payload["payload"]["run_state"]["direct_value_stage"]
                _verify_files(directory, report)
                restoration = load_checkpoint(checkpoint_path, model, optimizer,
                    expected_identity=identity, device="cpu")
                if (policy_function_sha(model) != identity["policy_function_sha"]
                        or restoration["optimizer_steps"] != optimizer_steps + report["optimizer_steps"]):
                    raise ValueError("direct value checkpoint restoration differs")
            else:
                training, train_evidence = _collect_rows(directory, config=config,
                    identity=identity, model=model, kind="training", count=plan["train_games"],
                    actual_port=actual_port, belief_port=belief_port, transport=transport, progress=progress)
                validation, validation_evidence = _collect_rows(directory, config=config,
                    identity=identity, model=model, kind="validation", count=plan["validation_games"],
                    actual_port=actual_port, belief_port=belief_port, transport=transport, progress=progress)
                if semantic_digest(model.state_dict()) != identity["input_model_sha"]:
                    raise ValueError("direct collection changed the frozen model")
                value_optimizer = create_value_optimizer(model, head_only=True)
                _copy_value_optimizer_state(model, optimizer, value_optimizer)
                update = train_value_generation(model, value_optimizer, training, validation,
                    seed=config["seed"] ^ generation ^ 0x56414C55, head_only=True)
                if policy_function_sha(model) != identity["policy_function_sha"]:
                    raise ValueError("direct value stage changed the policy function")
                if update["status"] == "updated":
                    _copy_value_optimizer_state(model, value_optimizer, optimizer)
                report = dict(schema=STAGE_SCHEMA, plan=plan, status=update["status"],
                    optimizer_steps=update["optimizer_steps"], update=update,
                    input_model_sha=identity["input_model_sha"],
                    output_model_sha=semantic_digest(model.state_dict()),
                    policy_function_sha=identity["policy_function_sha"], policy_unchanged=True,
                    training=train_evidence, validation=validation_evidence,
                    search_roots=0, value_sample_probability=.5)
                if training and validation:
                    # These helpers resample complete episodes, not correlated decision rows.
                    from .value_experiment import _constant_predictions, _bootstrap_ce_difference
                    predictions = value_predictions(model, validation)
                    constant = _constant_predictions(training, validation)
                    report["validation_metrics"] = prediction_metrics(predictions)
                    report["constant_baseline"] = prediction_metrics(constant)
                    report["ce_difference_vs_constant"] = _bootstrap_ce_difference(
                        predictions, constant, seed=config["seed"] ^ generation ^ 0x43414C49)
                save_checkpoint(checkpoint_path, model, optimizer, identity=identity,
                    optimizer_steps=optimizer_steps + report["optimizer_steps"],
                    run_state=dict(direct_value_stage=report))
                restoration = load_checkpoint(checkpoint_path, model, optimizer,
                    expected_identity=identity, device="cpu")
            if restoration["model_sha"] != report["output_model_sha"]:
                raise ValueError("direct value checkpoint model checksum differs")
            result = dict(report, checkpoint=dict(path=str(checkpoint_path),
                file_sha=restoration["file_sha"], model_sha=restoration["model_sha"]),
                checkpoint_roundtrip=True)
            _atomic_json(directory / "result.json", result)
            _atomic_json(directory / "status.json", dict(status="complete",
                stage="direct_value_complete", generation=generation,
                update_status=result["status"]))
            return result
        finally:
            for name, parameter in model.named_parameters():
                parameter.requires_grad_(flags[name])
            model.eval()
