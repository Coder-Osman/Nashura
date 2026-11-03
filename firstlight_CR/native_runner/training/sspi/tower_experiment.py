"""Independent 32/8/8 native tower-HP warmup. Never resumes an SSPI generation."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time
import traceback

import torch

from ..fair_search.contracts import semantic_digest
from ..v4.factory import production_semantic_bundle
from . import tower_bootstrap as warmup
from .augmentation import deserialize_information, serialize_information
from .collector import (_source_sha, _atomic_torch, _make_environment,
    EventBatchedSSPINative, preflight_native_endpoints, episode_seed_identity, VALUE_ARM)
from .learning import load_checkpoint, save_checkpoint
from .model import initialize_fresh_model
from .value_experiment import _atomic_json, _collect
from .value_learning import load_value_episode, policy_function_sha, policy_head_sha

SPLITS = {"training": 32, "validation": 8, "test": 8}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_identity():
    return semantic_digest(dict(collector=_source_sha(), files={name: sha(Path(__file__).with_name(name))
        for name in ("tower_bootstrap.py", "tower_experiment.py", "value_experiment.py", "value_learning.py")}))


def _status(run_dir, **record):
    _atomic_json(run_dir / "status.json", record)
    print(json.dumps(record, sort_keys=True, allow_nan=False), flush=True)


def _configuration(run_dir, run_id, model, optimizer, *, actual_port, belief_ports):
    specification = dict(schema=warmup.RUN_SCHEMA, run_id=run_id, seed=warmup.SEED,
        splits=SPLITS, sample_probability=.5, search_probability=0., device="cpu",
        formula=warmup.FORMULA, target_schema=warmup.TARGET_SCHEMA,
        source_sha=source_identity(), collector_source_sha=_source_sha(),
        initial_model_sha=semantic_digest(model.state_dict()), frozen_sha=policy_function_sha(model),
        initial_policy_head_sha=policy_head_sha(model), architecture_sha=model.architecture_sha,
        value_head_parameters=131842, transport="event-json-delta",
        native_ports=dict(actual=actual_port, belief_by_owner={str(k): v for k, v in belief_ports.items()}),
        optimizer=dict(type="AdamW", lr=1e-3, weight_decay=1e-4, betas=[.9, .999], eps=1e-8),
        batch=256, max_epochs=20, patience=3, min_improvement=1e-6, gradient_clip=1.,
        acceptance=dict(mse_reduction=.5, mae_max=.05, direction_min=.8,
            advantage_abs_min=.05, advantage_games_min=4, advantage_states_min=100))
    path = run_dir / "run_config.json"
    if path.exists():
        config = json.loads(path.read_text())
        if any(config.get(k) != v for k, v in specification.items()):
            raise ValueError("existing tower run identity/specification differs")
        if sha(run_dir / "checkpoints/initial.pt") != config["initial_checkpoint"]["file_sha"]:
            raise ValueError("initial checkpoint checksum differs")
        return config
    native = EventBatchedSSPINative("127.0.0.1", actual_port, timeout=90., event_deltas=True)
    try:
        environment = _make_environment(native, production_semantic_bundle())
        attestation = preflight_native_endpoints("127.0.0.1", actual_port, belief_ports,
            transport="event-json-delta", actual_env_id=0,
            ruleset_manifest=environment.ruleset_manifest, include_belief=False)
    finally:
        native.close_transport()
    if (run_dir / "checkpoints/initial.pt").exists():
        raise RuntimeError("orphan initial checkpoint requires inspection")
    identity = dict(protocol=warmup.RUN_SCHEMA, run_id=run_id, seed=warmup.SEED,
        source_sha=specification["source_sha"], architecture_sha=model.architecture_sha,
        initialization="fresh-random-no-inherited-weights", future_joint_optimizer="reinitialize")
    initial = save_checkpoint(run_dir / "checkpoints/initial.pt", model, optimizer,
        identity=identity, optimizer_steps=0)
    config = dict(**specification, engine_sha256=attestation["actual"]["libg_sha256"],
        native_attestation=attestation, checkpoint_identity=identity, initial_checkpoint=initial)
    _atomic_json(path, config)
    return config


def _episode_cache(run_dir, config, model, split, index, *, actual_port, belief_ports):
    game_id = f"tower-{split}-{index:04d}"
    kind = "training" if split == "training" else "validation"
    directory, metadata = _collect(run_dir, config, game_id, kind, model,
        actual_port=actual_port, belief_port=belief_ports[0], belief_port_owner_1=belief_ports[1],
        transport="event-json-delta", tower_hp_targets=True)
    if metadata["identity"].get("heuristic_target_schema") != warmup.TARGET_SCHEMA:
        raise ValueError("missing independent tower target protocol")
    expected_seed = episode_seed_identity(warmup.SEED, run_id=config["run_id"], arm=VALUE_ARM, game_id=game_id)
    if any(metadata["identity"].get(k) != v for k, v in expected_seed.items()):
        raise ValueError("tower split random stream identity differs")
    cache_path = run_dir / "cache" / f"{game_id}.pt"
    receipt_path = cache_path.with_suffix(".json")
    identity = dict(run_id=config["run_id"], split=split, game_id=game_id,
        source_sha=config["source_sha"], episode_sha=sha(directory / "episode.json"),
        tensor_sha=metadata["tensor_sha256"], action_sha=metadata["actions_sha256"])
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt["identity"] != identity or sha(cache_path) != receipt["file_sha"]:
            raise ValueError("tower cache receipt differs")
        cache = torch.load(cache_path, weights_only=True)
        if cache["schema"] != warmup.CACHE_SCHEMA or cache["shared_sha"] != config["frozen_sha"]:
            raise ValueError("cached shared representation identity differs")
        return cache, receipt
    rows, _ = load_value_episode(directory, expected_kind=kind,
        run_id=config["run_id"], model_sha=config["initial_model_sha"])
    raw = torch.load(directory / "episode.pt", weights_only=True)["value_rows"]
    started = time.perf_counter()
    cache = warmup.cache_rows(model, rows, raw)
    # Fixed public probes from training/validation only. No held-out selection.
    if rows and split != "test" and index <= 2:
        cache["policy_probe"] = dict(information=serialize_information(rows[0].information),
            signature=warmup.policy_signature(model, rows[0].information))
    checksum = _atomic_torch(cache_path, cache)
    receipt = dict(identity=identity, file_sha=checksum, samples=len(cache["records"]),
        excluded_states=cache["excluded"], decisive=metadata["result"]["winner"] is not None,
        wall_seconds=metadata["elapsed_seconds"], cache_seconds=time.perf_counter() - started,
        cache_forward_max_abs_error=cache["cache_forward_max_abs_error"],
        actual_match_seed=expected_seed["actual_match_seed"],
        actual_rollout_seed=expected_seed["actual_rollout_seed"],
        decisions=metadata["decisions"], timing_profile=metadata["timing_profile"],
        native_capture_performance=metadata["native_capture_performance"], search_roots=metadata["search_roots"])
    _atomic_json(receipt_path, receipt)
    return cache, receipt


def _collect_split(run_dir, config, model, split, *, actual_port, belief_ports):
    caches, receipts = [], []
    for index in range(1, SPLITS[split]+1):
        _status(run_dir, status="running", stage="collection", split=split,
            completed=index-1, target=SPLITS[split], active_game=f"tower-{split}-{index:04d}")
        cache, receipt = _episode_cache(run_dir, config, model, split, index,
            actual_port=actual_port, belief_ports=belief_ports)
        caches.append(cache)
        receipts.append(receipt)
        _status(run_dir, status="running", stage="episode_cached", split=split,
            completed=index, target=SPLITS[split], samples=sum(r["samples"] for r in receipts),
            wall_seconds=receipt["wall_seconds"], cache_seconds=receipt["cache_seconds"])
    combined = dict(schema=warmup.CACHE_SCHEMA, shared_sha=config["frozen_sha"],
        shared=torch.cat([c["shared"] for c in caches]), records=[r for c in caches for r in c["records"]])
    summary = dict(collected_games=len(receipts), decisive_games=sum(r["decisive"] for r in receipts),
        excluded_draw_games=sum(not r["decisive"] for r in receipts),
        invalid_states=sum(len(r["excluded_states"]) for r in receipts), samples=len(combined["records"]),
        mean_game_seconds=sum(r["wall_seconds"] for r in receipts)/len(receipts),
        cache_forward_max_abs_error=max(r["cache_forward_max_abs_error"] for r in receipts),
        receipts=receipts)
    _atomic_json(run_dir / f"{split}-collection.json", summary)
    return combined, summary, [c["policy_probe"] for c in caches if "policy_probe" in c]


def run(*, run_dir, run_id, actual_port=26889, belief_port=26893, belief_port_owner_1=26894):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    model = initialize_fresh_model(warmup.SEED, device="cpu").eval()
    optimizer = warmup.create_optimizer(model)
    belief_ports = {0: belief_port, 1: belief_port_owner_1}
    config = _configuration(run_dir, run_id, model, optimizer,
        actual_port=actual_port, belief_ports=belief_ports)
    report_path = run_dir / "result.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if (report["run_id"] != run_id or report["config_sha"] != sha(run_dir / "run_config.json")
                or report["checkpoint"]["file_sha"] != sha(run_dir / "checkpoints/best.pt")):
            raise ValueError("completed tower report identity differs")
        return report  # No new collection, epochs, or held-out evaluation.
    datasets, collections, probes = {}, {}, []
    for split in SPLITS:
        datasets[split], collections[split], selected_probes = _collect_split(run_dir, config, model,
            split, actual_port=actual_port, belief_ports=belief_ports)
        probes.extend(selected_probes)
    games = [{r["identity"]["game_id"] for r in c["receipts"]} for c in collections.values()]
    seeds = [{r["actual_match_seed"] for r in c["receipts"]} for c in collections.values()]
    if any(games[a] & games[b] or seeds[a] & seeds[b] for a in range(3) for b in range(a+1, 3)):
        raise ValueError("tower train/validation/test seed or episode overlap")
    if not datasets["training"]["records"] or not datasets["validation"]["records"]:
        record = dict(status="skipped", reason="empty effective train/validation set", collections=collections)
        _atomic_json(run_dir / "skipped.json", record)
        _status(run_dir, **{k: v for k, v in record.items() if k != "collections"})
        return record
    optimizer = warmup.create_optimizer(model)
    initial_head_sha = semantic_digest(model.value_head.state_dict())
    training_identity = dict(**config["checkpoint_identity"], phase="head-warmup",
        data_sha=semantic_digest({s: c["receipts"] for s, c in collections.items()}))
    update = warmup.train_head(model, optimizer, datasets["training"], datasets["validation"],
        checkpoint_path=run_dir / "checkpoints/progress.pt", identity=training_identity,
        epoch_callback=lambda row: _status(run_dir, status="running", stage="training", **row))
    policy_checks = [warmup.policy_signature(model, deserialize_information(p["information"])) == p["signature"]
        for p in probes]
    frozen_ok = policy_function_sha(model) == config["frozen_sha"]
    head_changed = semantic_digest(model.value_head.state_dict()) != initial_head_sha
    best_receipt = run_dir / "checkpoints/best.json"
    if best_receipt.exists():
        best = json.loads(best_receipt.read_text())
        if (best["file_sha"] != sha(run_dir / "checkpoints/best.pt")
                or best["model_sha"] != semantic_digest(model.state_dict())):
            raise ValueError("selected best checkpoint differs on resume")
    else:
        if (run_dir / "checkpoints/best.pt").exists():
            # Recover the narrow crash window after atomic checkpoint commit.
            verifier = initialize_fresh_model(warmup.SEED, device="cpu").eval()
            verification_optimizer = warmup.create_optimizer(verifier)
            verified = load_checkpoint(run_dir / "checkpoints/best.pt", verifier,
                verification_optimizer, expected_identity=training_identity)
            if (semantic_digest((verifier.state_dict(), verification_optimizer.state_dict()))
                    != semantic_digest((model.state_dict(), optimizer.state_dict()))
                    or verified["run_state"]["update"] != update):
                raise ValueError("orphan best checkpoint differs from the completed update")
            best = dict(path=str(run_dir / "checkpoints/best.pt"), file_sha=verified["file_sha"],
                model_sha=verified["model_sha"], architecture_sha=model.architecture_sha)
        else:
            best = save_checkpoint(run_dir / "checkpoints/best.pt", model, optimizer,
                identity=training_identity, optimizer_steps=update["selected_steps"],
                run_state=dict(update=update, intended_use="heuristic-initialization-only", future_joint_optimizer="reinitialize"))
        _atomic_json(best_receipt, best)
    restored = initialize_fresh_model(warmup.SEED, device="cpu").eval()
    restored_optimizer = warmup.create_optimizer(restored)
    load_checkpoint(run_dir / "checkpoints/best.pt", restored, restored_optimizer, expected_identity=training_identity)
    roundtrip = semantic_digest((model.state_dict(), optimizer.state_dict())) == semantic_digest(
        (restored.state_dict(), restored_optimizer.state_dict()))
    if not frozen_ok or not all(policy_checks) or not roundtrip:
        raise ValueError("tower freeze/policy/checkpoint integrity failed")
    # Only this immutable record unlocks the single final held-out evaluation.
    selection = dict(checkpoint_sha=best["file_sha"], model_sha=best["model_sha"],
        data_sha=training_identity["data_sha"], best_epoch=update["best_epoch"])
    selection_path = run_dir / "selection.json"
    if selection_path.exists() and json.loads(selection_path.read_text()) != selection:
        # torch.save includes RNG state. Existing selection must identify the same exact checkpoint.
        raise ValueError("held-out model selection identity differs")
    _atomic_json(selection_path, selection)
    holdout_path = run_dir / "holdout.json"
    if holdout_path.exists():
        holdout = json.loads(holdout_path.read_text())
        if holdout["selection"] != selection:
            raise ValueError("held-out evaluation belongs to another selected checkpoint")
    else:
        target_mean = sum(r["score"] for r in datasets["training"]["records"]) / len(datasets["training"]["records"])
        records = datasets["test"]["records"]
        current = warmup.score_metrics(records, warmup.cached_scores(restored, datasets["test"]))
        zero = warmup.score_metrics(records, [0.] * len(records))
        mean = warmup.score_metrics(records, [target_mean] * len(records))
        holdout = dict(selection=selection, current=current, zero_baseline=zero,
            training_mean_baseline=mean, training_mean_score=target_mean,
            acceptance=warmup.acceptance(current, zero, mean,
                integrity=frozen_ok and all(policy_checks) and roundtrip))
        _atomic_json(holdout_path, holdout)
    report = dict(schema=warmup.RUN_SCHEMA, run_id=run_id, status="complete",
        config_sha=sha(run_dir / "run_config.json"), source_sha=config["source_sha"],
        initial_model_sha=config["initial_model_sha"], checkpoint=best, checkpoint_identity=training_identity,
        collections=collections, update=update, holdout=holdout, search_roots=0,
        integrity=dict(frozen_sha=config["frozen_sha"], frozen_unchanged=frozen_ok,
            fixed_policy_probes=len(probes), policy_unchanged=all(policy_checks),
            head_changed=head_changed, checkpoint_roundtrip=roundtrip))
    _atomic_json(report_path, report)
    _status(run_dir, status="complete", stage="finished", passed=holdout["acceptance"]["passed"],
        best_epoch=update["best_epoch"], checkpoint_sha=best["file_sha"], holdout=holdout["acceptance"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actual-port", type=int, default=26889)
    parser.add_argument("--belief-port", type=int, default=26893)
    parser.add_argument("--belief-port-owner-1", type=int, default=26894)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    with (args.run_dir / "orchestrator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            run(**vars(args))
        except BaseException as error:
            _status(args.run_dir, status="paused_failure", exception=type(error).__name__,
                reason=str(error), traceback=traceback.format_exc())
            raise


if __name__ == "__main__":
    main()
