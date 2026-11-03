"""One preregistered random-start SSPI run; no architecture or parameter sweep."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import random
import subprocess
import time
import traceback

import torch

from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import semantic_digest
from ..v4.factory import production_semantic_bundle
from . import experiment as exp
from .collector import _source_sha, episode_seed_identity, preflight_native_endpoints
from .learning import (UpdateRejected, create_optimizer, load_checkpoint,
    load_episode_training_rows, load_episode_validation_rows, save_checkpoint,
    train_generation_step)
from .model import initialize_fresh_model
from .protocol import ActionMode
from .value_calibration import run_direct_value_stage


PROTOCOL = dict(schema="sspi-random60.v1", seed=20261103, generations=8,
    search_probability=.05, simulations=128, joint_lr=1e-5, weight_decay=1e-4,
    maximum_epochs=2, batch_size=128, microbatch=4, clip=1.,
    search_games_early=[4, 2], search_games_later=[8, 4],
    direct_value_stage=dict(schema="sspi-direct-value-stage.v1", train_games=32,
        validation_games=16), screening_generations=[1, 2, 4, 8], screening_pairs=64,
    selection_threshold=.65, fallback_threshold=.60, minimum_decisive=64,
    formal_minimum_pairs=64, formal_maximum_pairs=1500,
    evaluation_temperature=1., failed_episode="pause_without_replacement")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sources():
    root = Path(__file__).parent
    return dict(collector=_source_sha(), **{name: sha(root / name) for name in (
        "random60.py", "learning.py", "experiment.py", "value_calibration.py",
        "value_learning.py", "value_experiment.py")})


def all_root_batches(count, *, seed, epochs=2, batch_size=128):
    if (any(type(x) is not int or x < 1 for x in (count, epochs, batch_size))
            or type(seed) is not int):
        raise ValueError("invalid all-root schedule")
    rng, batches = random.Random(seed), []
    for epoch in range(epochs):
        order = list(range(count))
        rng.shuffle(order)
        batches.extend(dict(epoch=epoch + 1, indices=order[offset:offset + batch_size])
            for offset in range(0, count, batch_size))
    return batches


def choose_candidate(screens, *, last_generation):
    eligible = [record for record in screens if record["score"]["decisive_games"] >= 64
        and record["score"]["mean"] is not None]
    early = [record for record in eligible if record["score"]["mean"] >= .65]
    if early:
        return min(early, key=lambda record: record["generation"])
    if last_generation == 8:
        eligible = [record for record in eligible if record["score"]["mean"] >= .60]
        if eligible:
            return max(eligible, key=lambda record: (record["score"]["mean"], -record["generation"]))
    return None


def mode_probe(model, rows):
    """Read existing states only; report forced WAIT separately from actionable WAIT."""
    model.eval()
    results = []
    with torch.inference_mode():
        for row in rows[:32]:
            policies, _ = model(row.frame, [row.legality])
            probabilities = policies[0].conditionals().exp().cpu().tolist()
            results.append(dict(fair_sha=row.frame.sha256, probabilities=probabilities,
                has_play=bool(row.legality.plays.any())))
    actionable = [row["probabilities"][int(ActionMode.WAIT)] for row in results if row["has_play"]]
    return dict(states=results, actionable_states=len(actionable), forced_wait_states=len(results)-len(actionable),
        actionable_wait_mean=sum(actionable)/len(actionable) if actionable else None)


def mode_drift(before, after):
    if [r["fair_sha"] for r in before["states"]] != [r["fair_sha"] for r in after["states"]]:
        raise ValueError("mode drift probe states changed")
    return dict(metric="mean_mode_total_variation_not_full_joint_KL",
        value=sum(sum(abs(p-q) for p,q in zip(a["probabilities"],b["probabilities"]))/2
            for a,b in zip(before["states"],after["states"]))/len(before["states"]))


def initialize_run(directory, *, actual_port, belief_ports):
    """Create fresh weights and a run manifest after read-only endpoint preflight."""
    directory = Path(directory).resolve()
    if directory.exists():
        raise ValueError("new experiment requires an unused run directory")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    bundle = production_semantic_bundle()
    model = initialize_fresh_model(PROTOCOL["seed"], device="cpu", bundle=bundle).eval()
    optimizer = create_optimizer(model, lr=PROTOCOL["joint_lr"])
    attestations = preflight_native_endpoints("127.0.0.1", actual_port, belief_ports,
        transport="event-json-delta")
    directory.mkdir(parents=True, exist_ok=False)
    source = sources()
    config = dict(schema="sspi-mac-pilot-run.v2", experiment_schema=PROTOCOL["schema"],
        run_id=directory.name, status="collecting", seed=PROTOCOL["seed"], arm="A_5PCT_128",
        device="cpu", deck=list(HOG_DECK), actual_env_id=0, transport="event-json-delta",
        temporal_augmentation=True, protocol=PROTOCOL, protocol_sha=semantic_digest(PROTOCOL),
        ports=dict(actual=actual_port, belief_by_owner={str(k): v for k,v in belief_ports.items()}),
        engine_sha256=attestations["actual"]["libg_sha256"], native_worker_attestations=attestations,
        collector_source_sha=source["collector"], source=source,
        direct_value_stage=PROTOCOL["direct_value_stage"],
        created_unix=time.time(), parameter_count=sum(p.numel() for p in model.parameters()),
        runtime=dict(python=platform.python_version(), torch=torch.__version__,
            machine=platform.machine(), threads=1, deterministic_algorithms=True),
        git_head=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent, text=True).strip())
    identity = dict(run_id=directory.name, schema=PROTOCOL["schema"], generation=0,
        seed=PROTOCOL["seed"], protocol_sha=config["protocol_sha"], source=source,
        engine_sha256=config["engine_sha256"])
    config["initialization_identity"] = identity
    config["initialization"] = save_checkpoint(directory / "initialization.pt", model,
        optimizer, identity=identity, optimizer_steps=0)
    exp._atomic_json(directory / "run_config.json", config)
    exp._write_status(directory, "ready", "initialized", generation=0,
        model_sha=config["initialization"]["model_sha"])
    return config


def validate_config(config):
    if (config.get("experiment_schema") != PROTOCOL["schema"]
            or config.get("protocol") != PROTOCOL
            or config.get("protocol_sha") != semantic_digest(PROTOCOL)
            or config.get("source") != sources() or config.get("seed") != PROTOCOL["seed"]
            or config.get("device") != "cpu" or config.get("deck") != list(HOG_DECK)
            or config.get("runtime") != dict(python=platform.python_version(), torch=torch.__version__,
                machine=platform.machine(), threads=1, deterministic_algorithms=True)
            or config.get("direct_value_stage") != PROTOCOL["direct_value_stage"]):
        raise ValueError("frozen run protocol or source changed; retain original deployment")


def collect_split(directory, config, generation, model, *, kind, count):
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model_sha = semantic_digest(model.state_dict())
    rows, files, outcomes = [], {}, dict(collected_games=0, decisive_games=0, excluded_draw_games=0)
    for index in range(1, count + 1):
        game_id = f"{'A' if kind == 'training' else 'V'}-g{generation:04d}-game-{index:04d}"
        target = directory / "episodes" / game_id
        exp._write_status(directory, "running", f"search_{kind}_collection",
            generation=generation, game_id=game_id, games=index-1, target_games=count,
            search_roots=len(rows))
        if target.exists():
            if not (target / "episode.json").is_file() or (target / "quarantine.json").exists():
                raise RuntimeError(f"incomplete episode retained for inspection: {target}")
            metadata = json.loads((target / "episode.json").read_text())
        else:
            _, metadata = exp._collect_one(run_config=config, run_dir=directory, game_id=game_id,
                episode_kind=kind, model=model, device="cpu", actual_port=config["ports"]["actual"],
                belief_port=config["ports"]["belief_by_owner"]["0"], transport=config["transport"])
        exp._validate_episode(metadata, run_config=config, model_sha=model_sha,
            episode_kind=kind, search_probability=.05)
        identity = metadata["identity"]
        expected_seed = episode_seed_identity(config["seed"], run_id=config["run_id"],
            arm=config["arm"], game_id=game_id)
        if (identity.get("game_id") != game_id or identity.get("source_sha256") != config["collector_source_sha"]
                or identity.get("engine_sha256") != config["engine_sha256"]
                or identity.get("native_worker_attestations") != config["native_worker_attestations"]
                or metadata.get("search_simulations") != 128
                or any(identity.get(k) != v for k,v in expected_seed.items())):
            raise ValueError("collected episode differs from frozen source, seeds or engine")
        loader = load_episode_training_rows if kind == "training" else load_episode_validation_rows
        rows.extend(loader(target))
        for name in ("episode.json", "episode.pt", "actions.json"):
            files[str((target / name).relative_to(directory))] = sha(target / name)
        outcomes["collected_games"] += 1
        outcomes["decisive_games" if exp.decisive_result(metadata["result"]) else "excluded_draw_games"] += 1
    return rows, dict(files=files, outcomes=outcomes)


def train_all_roots(directory, config, generation, model, optimizer, rows, validation,
        *, input_steps, data_identity):
    """Persist every attempted batch. An uncertain write pauses instead of replaying exposure."""
    if not rows or not validation:
        raise ValueError("generation has no eligible search training or validation rows")
    target = directory / "joint" / f"generation-{generation:04d}"
    target.mkdir(parents=True, exist_ok=True)
    schedule = all_root_batches(len(rows), seed=config["seed"] ^ generation ^ 0x53535049)
    identity = dict(schema="sspi-all-root-update.v1", run_id=config["run_id"], generation=generation,
        config_sha=semantic_digest(config), input_model_sha=semantic_digest(model.state_dict()),
        input_optimizer_sha=semantic_digest(optimizer.state_dict()), input_steps=input_steps,
        data=data_identity, schedule_sha=semantic_digest(schedule))
    state = dict(next_batch=0, attempted_counts=[0]*len(rows), accepted_counts=[0]*len(rows),
        updates=[], stopped_on_rejection=False, optimizer_steps=0)
    checkpoint_path = target / "checkpoint.pt"
    if checkpoint_path.exists():
        restored = load_checkpoint(checkpoint_path, model, optimizer, expected_identity=identity)
        state = restored["run_state"]
        if restored["optimizer_steps"] != input_steps + state["optimizer_steps"]:
            raise ValueError("joint checkpoint optimizer count differs")
    for parameter in model.parameters():
        parameter.requires_grad_(True)
    while state["next_batch"] < len(schedule) and not state["stopped_on_rejection"]:
        number = state["next_batch"] + 1
        batch = schedule[number-1]
        intent = target / f"attempt-{number:04d}.json"
        if intent.exists():
            raise RuntimeError(f"uncommitted update attempt requires inspection: {intent}")
        exp._atomic_json(intent, dict(identity_sha=semantic_digest(identity), batch=batch,
            input_model_sha=semantic_digest(model.state_dict()), input_optimizer_sha=semantic_digest(optimizer.state_dict())))
        exp._write_status(directory, "running", "joint_update", generation=generation,
            batch=number, total_batches=len(schedule), roots=len(batch["indices"]))
        for index in batch["indices"]:
            state["attempted_counts"][index] += 1
        try:
            update = train_generation_step(model, optimizer, [rows[i] for i in batch["indices"]],
                validation_rows=validation, allow_partial=True)
        except UpdateRejected as error:
            update = dict(status="rejected", reason=str(error), optimizer_steps=0, evidence=error.report)
            state["stopped_on_rejection"] = True
        else:
            update["status"] = "accepted"
            state["optimizer_steps"] += 1
            for index in batch["indices"]:
                state["accepted_counts"][index] += 1
        state["updates"].append(dict(batch=batch, result=update))
        state["next_batch"] = number
        save_checkpoint(checkpoint_path, model, optimizer, identity=identity,
            optimizer_steps=input_steps + state["optimizer_steps"], run_state=state)
        restored = load_checkpoint(checkpoint_path, model, optimizer, expected_identity=identity)
        if restored["run_state"] != state:
            raise ValueError("joint checkpoint roundtrip differs")
        exp._atomic_json(target / "result.json", state)
        exp._emit("joint_update_complete", generation=generation, batch=number, **update)
    model.eval()
    return state


def restore_record(directory, config, generation, model, optimizer):
    record = json.loads((directory / f"generation-{generation:04d}.json").read_text())
    if (record["config_sha"] != semantic_digest(config) or record["generation"] != generation
            or sha(record["checkpoint"]["path"]) != record["checkpoint"]["file_sha"]):
        raise ValueError("generation record identity or checkpoint changed")
    for split in ("training", "validation"):
        for path, expected in record[split]["files"].items():
            if sha(directory / path) != expected:
                raise ValueError("completed generation data changed")
    restored = load_checkpoint(record["checkpoint"]["path"], model, optimizer,
        expected_identity=record["checkpoint_identity"])
    if restored["run_state"].get("generation_record") != {
            k:v for k,v in record.items() if k not in ("checkpoint", "checkpoint_roundtrip")}:
        raise ValueError("generation JSON differs from committed checkpoint record")
    return record, restored["optimizer_steps"]


def run_generation(directory, config, generation, model, optimizer, input_steps):
    record_path = directory / f"generation-{generation:04d}.json"
    checkpoint_path = directory / "checkpoints" / f"generation-{generation:04d}.pt"
    if record_path.exists():
        return restore_record(directory, config, generation, model, optimizer)
    if checkpoint_path.exists():
        # The atomic checkpoint commits the generation before its readable JSON record.
        envelope = torch.load(checkpoint_path, weights_only=True, map_location="cpu")
        record = envelope["payload"]["run_state"]["generation_record"]
        restored = load_checkpoint(checkpoint_path, model, optimizer,
            expected_identity=record["checkpoint_identity"])
        record["checkpoint"] = dict(path=str(checkpoint_path), file_sha=restored["file_sha"],
            model_sha=restored["model_sha"], architecture_sha=model.architecture_sha)
        record["checkpoint_roundtrip"] = True
        exp._atomic_json(record_path, record)
        return restore_record(directory, config, generation, model, optimizer)
    input_sha = semantic_digest(model.state_dict())
    train_count, validation_count = (PROTOCOL["search_games_early"] if generation <= 2
        else PROTOCOL["search_games_later"])
    rows, train_data = collect_split(directory, config, generation, model, kind="training", count=train_count)
    held, validation_data = collect_split(directory, config, generation, model, kind="validation", count=validation_count)
    if not rows or not held:
        exp._atomic_json(directory / f"generation-{generation:04d}-skipped.json", dict(
            status="skipped", reason="no_effective_training_or_validation_roots", generation=generation,
            training=train_data, validation=validation_data, optimizer_steps=0))
        raise RuntimeError("generation update skipped: no effective training or validation roots")
    probe_rows = held
    if generation > 1:
        probe_rows = []
        for path in sorted((directory / "episodes").glob("V-g0001-game-*")):
            probe_rows.extend(load_episode_validation_rows(path))
        if not probe_rows:
            raise ValueError("fixed generation-one policy probe is empty")
    probe_before = mode_probe(model, probe_rows)
    joint = train_all_roots(directory, config, generation, model, optimizer, rows, held,
        input_steps=input_steps, data_identity=dict(training=train_data, validation=validation_data))
    probe_joint = mode_probe(model, probe_rows)
    del rows, held
    steps = input_steps + joint["optimizer_steps"]
    stage = run_direct_value_stage(run_dir=directory, config=config, generation=generation,
        model=model, optimizer=optimizer, optimizer_steps=steps, engine_sha=config["engine_sha256"],
        actual_port=config["ports"]["actual"], belief_port=config["ports"]["belief_by_owner"]["0"],
        transport=config["transport"], progress=lambda row: exp._write_status(directory,
            row["status"], row["stage"], **{k:v for k,v in row.items() if k not in ("status", "stage")}))
    steps += stage["optimizer_steps"]
    probe_final = mode_probe(model, probe_rows)
    if probe_final != probe_joint:
        raise ValueError("head-only stage changed CPU fixed-state policy probabilities")
    identity = dict(schema=PROTOCOL["schema"], run_id=config["run_id"], generation=generation,
        config_sha=semantic_digest(config), input_model_sha=input_sha, input_steps=input_steps,
        training=train_data, validation=validation_data, joint=joint, direct_value_stage=stage)
    record = dict(schema="sspi-random60-generation.v1", config_sha=semantic_digest(config),
        generation=generation, checkpoint_identity=identity, training=train_data,
        validation=validation_data, joint=joint, direct_value_stage=stage, optimizer_steps=steps,
        policy_diagnostics=dict(before=probe_before, after_joint=probe_joint, after_value=probe_final,
            joint_mode_drift=mode_drift(probe_before,probe_joint), value_policy_unchanged=True),
        theta0_sha256=config["initialization"]["model_sha"])
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer, identity=identity,
        optimizer_steps=steps, run_state=dict(generation_record=record))
    restored = load_checkpoint(checkpoint_path, model, optimizer, expected_identity=identity)
    if restored["model_sha"] != checkpoint["model_sha"]:
        raise ValueError("generation checkpoint roundtrip differs")
    record.update(checkpoint=checkpoint, checkpoint_roundtrip=True)
    exp._atomic_json(record_path, record)
    exp._emit("generation_complete", generation=generation, checkpoint=checkpoint,
        joint_steps=joint["optimizer_steps"], direct_value_steps=stage["optimizer_steps"])
    return record, steps


def evaluate(directory, config, record, model, baseline, *, domain):
    for network in (model, baseline):
        network.eval()
        for parameter in network.parameters():
            parameter.requires_grad_(False)
    return exp._run_paired_evaluation(run_config=config, run_dir=directory,
        generation=record["generation"], trained_model=model,
        trained_model_sha=record["checkpoint"]["model_sha"], theta0_model=baseline,
        theta0_sha=config["initialization"]["model_sha"], checkpoint_sha=record["checkpoint"]["file_sha"],
        actual_port=config["ports"]["actual"], belief_port=config["ports"]["belief_by_owner"]["0"],
        transport=config["transport"], evaluation_pairs=(1500 if domain == "formal" else 64),
        output_name=f"evaluation-{domain}-generation-{record['generation']:04d}.json",
        evaluation_domain=domain, sequential=domain == "formal", minimum_pairs=64)


def run(directory):
    directory = Path(directory).resolve()
    config = json.loads((directory / "run_config.json").read_text())
    validate_config(config)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    with (directory / ".run.lock").open("a") as run_lock, (
            directory.parent / f".sspi-native-port-{config['ports']['actual']}.lock").open("a") as engine_lock:
        fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(engine_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bundle = production_semantic_bundle()
        baseline = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle).eval()
        model = initialize_fresh_model(config["seed"], device="cpu", bundle=bundle)
        optimizer = create_optimizer(model, lr=PROTOCOL["joint_lr"])
        initial = config["initialization"]
        if sha(initial["path"]) != initial["file_sha"] or semantic_digest(baseline.state_dict()) != initial["model_sha"]:
            raise ValueError("random initialization identity changed")
        load_checkpoint(initial["path"], model, optimizer, expected_identity=config["initialization_identity"])
        screens, steps, records = [], 0, []
        for generation in range(1, 9):
            record, steps = run_generation(directory, config, generation, model, optimizer, steps)
            records.append(record)
            if generation in PROTOCOL["screening_generations"]:
                screens.append(evaluate(directory, config, record, model, baseline, domain="screening"))
            candidate = choose_candidate(screens, last_generation=generation)
            if candidate is not None:
                selected = candidate["generation"]
                record, steps = restore_record(directory, config, selected, model, optimizer)
                selection = dict(schema="sspi-random60-candidate.v1", generation=selected,
                    checkpoint=record["checkpoint"], config_sha=semantic_digest(config),
                    screening_sha=sha(directory / f"evaluation-screening-generation-{selected:04d}.json"))
                selection_path = directory / "formal-candidate.json"
                if selection_path.exists() and json.loads(selection_path.read_text()) != selection:
                    raise ValueError("formal candidate already locked to another checkpoint")
                exp._atomic_json(selection_path, selection)
                result = evaluate(directory, config, record, model, baseline, domain="formal")
                exp._write_status(directory, "complete", "formal_evaluation_complete", generation=selected,
                    passed=result["sequential_evidence"]["passed"], score=result["score"],
                    evidence=result["sequential_evidence"])
                return result
            if len(records) >= 2 and all(r["joint"]["optimizer_steps"] == 0 for r in records[-2:]):
                exp._write_status(directory, "needs_diagnosis", "two_generations_without_policy_update",
                    generation=generation)
                return None
        exp._write_status(directory, "complete", "eight_generations_without_formal_candidate", generation=8)
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--initialize-only", action="store_true")
    parser.add_argument("--actual-port", type=int, default=26889)
    parser.add_argument("--belief-port-owner-0", type=int, default=26893)
    parser.add_argument("--belief-port-owner-1", type=int, default=26894)
    args = parser.parse_args()
    if args.initialize_only:
        config = initialize_run(args.run_dir, actual_port=args.actual_port,
            belief_ports={0: args.belief_port_owner_0, 1: args.belief_port_owner_1})
        exp._emit("initialized", run_id=config["run_id"], initialization=config["initialization"])
        return
    try:
        run(args.run_dir)
    except BaseException as error:
        # Do not let a rejected duplicate launch overwrite a live actor's status.
        if not isinstance(error, BlockingIOError):
            exp._atomic_json(args.run_dir / f"failure-{time.time_ns()}.json", dict(
                exception=type(error).__name__, message=str(error), traceback=traceback.format_exc()))
            exp._write_status(args.run_dir, "failed", "experiment_failed", reason=str(error))
        raise


if __name__ == "__main__":
    main()
