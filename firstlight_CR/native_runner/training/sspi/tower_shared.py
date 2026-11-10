"""Diagnostic tower-HP fitting with the shared SSPI network unfrozen, no engine."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import random
import time
import traceback

import torch

from ..fair_search.contracts import pack, unpack, semantic_digest
from ..v4.tensors import concatenate_padded_tensor_records
from .augmentation import serialize_information, deserialize_information
from .collector import _atomic_torch
from .learning import save_checkpoint, load_checkpoint
from .model import initialize_fresh_model, _hand_features
from .tower_bootstrap import (SEED, TARGET_SCHEMA, validate_target, soft_loss, score_metrics)
from .value_experiment import _atomic_json
from .value_learning import load_value_episode, configure_value_training, policy_head_sha, policy_function_sha

DATA_SCHEMA = "sspi-tower-raw-inputs.v1"
RUN_SCHEMA = "sspi-tower-shared-fit.v2"
BATCH = 256
MICROBATCH = 16
LR = 1e-4
SIGNAL_STREAK = 3
MAX_EPOCHS = 8


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_sha():
    root = Path(__file__).resolve().parents[1]
    return semantic_digest({str(path.relative_to(root)): file_sha(path)
        for path in sorted(root.rglob("*.py"))})


def prepare(source_run, destination):
    """Export only the prior training/validation FAIR inputs, never learned embeddings/test data."""
    source_run, destination = Path(source_run), Path(destination)
    config = json.loads((source_run / "run_config.json").read_text())
    report = json.loads((source_run / "result.json").read_text())
    destination.mkdir(parents=True, exist_ok=True)
    identity = dict(schema=DATA_SCHEMA, source_run_id=config["run_id"],
        source_config_sha=file_sha(source_run / "run_config.json"),
        initial_model_sha=config["initial_model_sha"], seed=SEED, target_schema=TARGET_SCHEMA,
        original_sampling_probability=.5, source_splits={"training":32, "validation":8},
        heldout_exported=False)
    manifest_path = destination / "dataset.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["identity"] != identity:
            raise ValueError("existing raw dataset identity differs")
        for entry in manifest["shards"]:
            if file_sha(destination / entry["path"]) != entry["sha256"]:
                raise ValueError("exported raw shard checksum differs")
        if file_sha(destination / "initial.pt") != manifest["initial_file_sha"]:
            raise ValueError("exported random initialization differs")
        return manifest
    initial_path = source_run / "checkpoints/initial.pt"
    if file_sha(initial_path) != config["initial_checkpoint"]["file_sha"]:
        raise ValueError("original random initialization checksum differs")
    envelope = torch.load(initial_path, weights_only=True, map_location="cpu")
    if (semantic_digest(envelope["payload"]) != envelope["payload_sha"]
            or semantic_digest(envelope["payload"]["model"]) != identity["initial_model_sha"]):
        raise ValueError("initial state is not the recorded fresh random model")
    initial_sha = _atomic_torch(destination / "initial.pt", dict(schema=DATA_SCHEMA,
        identity=identity, architecture=envelope["payload"]["architecture"],
        model=envelope["payload"]["model"]))  # No old optimizer or device RNG state.
    shards = []
    for split in ("training", "validation"):
        for receipt in report["collections"][split]["receipts"]:
            game_id = receipt["identity"]["game_id"]
            directory = source_run / "episodes" / game_id
            if file_sha(directory / "episode.json") != receipt["identity"]["episode_sha"]:
                raise ValueError("original complete episode receipt differs")
            rows, metadata = load_value_episode(directory, expected_kind=split,
                run_id=config["run_id"], model_sha=identity["initial_model_sha"])
            raw = torch.load(directory / "episode.pt", weights_only=True)["value_rows"]
            frames, hands, records, probes = [], [], [], []
            for row, sample in zip(rows, raw):
                if not validate_target(sample["tower_hp_target"], sample):
                    continue
                frames.append(row.information.frame)
                hands.append(_hand_features(row.information.hand, device="cpu"))
                records.append(dict(game_id=game_id, decision_index=sample["decision_index"],
                    **sample["tower_hp_target"]))
                if len(records) in (1, len(rows)//2) and game_id.endswith(("0001", "0002")):
                    probes.append(serialize_information(row.information))
            name = f"{split}/{game_id}.pt"
            content = dict(schema=DATA_SCHEMA, split=split, game_id=game_id,
                source_episode_sha=file_sha(directory / "episode.json"),
                records=records, hand_features=torch.stack(hands) if hands else torch.empty(0, 128),
                frame=pack(concatenate_padded_tensor_records(tuple(frames))) if frames else None,
                probes=probes)
            checksum = _atomic_torch(destination / name, content)
            shards.append(dict(path=name, sha256=checksum, samples=len(records), split=split,
                game_id=game_id, natural_decisive=metadata["result"]["winner"] is not None,
                source_episode_sha=content["source_episode_sha"]))
            print(json.dumps(dict(stage="export", game_id=game_id, samples=len(records))), flush=True)
    manifest = dict(identity=identity, initial_file_sha=initial_sha, shards=shards,
        totals={split:sum(s["samples"] for s in shards if s["split"] == split)
            for split in ("training", "validation")})
    _atomic_json(manifest_path, manifest)
    return manifest


def load_data(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "dataset.json").read_text())
    if (manifest["identity"]["schema"] != DATA_SCHEMA or manifest["identity"]["heldout_exported"] is not False
            or file_sha(directory / "initial.pt") != manifest["initial_file_sha"]):
        raise ValueError("raw supervised data schema or initialization differs")
    data, probes, seen = {}, [], set()
    for split in ("training", "validation"):
        frames, hands, records = [], [], []
        entries = [s for s in manifest["shards"] if s["split"] == split]
        if len(entries) != manifest["identity"]["source_splits"][split]:
            raise ValueError("raw split episode count differs")
        for entry in entries:
            path = directory / entry["path"]
            if file_sha(path) != entry["sha256"] or entry["game_id"] in seen:
                raise ValueError("raw input checksum or episode split overlap")
            seen.add(entry["game_id"])
            shard = torch.load(path, weights_only=True, map_location="cpu")
            if (shard.get("schema") != DATA_SCHEMA or shard["split"] != split
                    or shard["game_id"] != entry["game_id"] or len(shard["records"]) != entry["samples"]):
                raise ValueError("raw input shard identity differs")
            if shard["records"]:
                frame = unpack(shard["frame"])
                if frame.batch_size != len(shard["records"]) or shard["hand_features"].shape != (frame.batch_size, 128):
                    raise ValueError("raw frame/hand batch differs")
                frames.append(frame)
                hands.append(shard["hand_features"])
                records.extend(shard["records"])
                probes.extend(shard["probes"])
        if not records:
            raise ValueError("empty effective supervised split")
        data[split] = dict(frame=concatenate_padded_tensor_records(tuple(frames)),
            hands=torch.cat(hands), records=records,
            targets=torch.tensor([r["soft_target"] for r in records], dtype=torch.float32))
    return data, probes, manifest


def create_optimizer(model):
    parameters = configure_value_training(model, head_only=False)
    expected = tuple(p for name, p in model.named_parameters() if not name.startswith("policy_head."))
    if {id(p) for p in parameters} != {id(p) for p in expected}:
        raise ValueError("unfrozen shared/value parameter scope differs")
    return torch.optim.AdamW(parameters, lr=LR, weight_decay=1e-4, betas=(.9,.999), eps=1e-8)


def predict(model, data):
    model.eval()
    predictions = []
    with torch.inference_mode():
        for start in range(0, len(data["records"]), MICROBATCH):
            count = min(MICROBATCH, len(data["records"])-start)
            logits = model.value_batch(data["frame"].narrow_batch(start, count), data["hands"][start:start+count])
            probabilities = logits.softmax(-1)
            predictions.extend((probabilities[:,0]-probabilities[:,1]).cpu().tolist())
    return predictions


def metrics(model, data):
    return score_metrics(data["records"], predict(model, data), replicates=0)


def policy_probe(model, probes):
    model.eval()
    values = []
    with torch.inference_mode():
        for raw in probes:
            info = deserialize_information(raw)
            policy = model(info, [info.legality])[0][0]
            values.append(policy.conditionals().exp().cpu().tolist())
    return values


def fit_signal(train, valid, baselines):
    return bool(train["mse"] <= .5*baselines["training"]
        and valid["mse"] <= .5*baselines["validation"] and valid["mae"] <= .05
        and valid["direction_accuracy"] is not None and valid["direction_accuracy"] >= .8
        and valid["advantage_games"] >= 4 and valid["advantage_states"] >= 100)


def fitting_complete(state):
    return state["streak"] >= SIGNAL_STREAK and fit_signal(state["best_metrics"]["training"],
        state["best_metrics"]["validation"], state["constants"])


def train_epoch(model, optimizer, data, epoch, *, device):
    model.train()
    order = list(range(len(data["records"])))
    random.Random(SEED ^ (0x53484152 + epoch)).shuffle(order)
    total, samples, steps, first_gradients = 0., 0, 0, None
    parameters = [p for p in model.parameters() if p.requires_grad]
    for start in range(0, len(order), BATCH):
        batch = order[start:start+BATCH]
        optimizer.zero_grad(set_to_none=True)
        for part in range(0, len(batch), MICROBATCH):
            indices = torch.tensor(batch[part:part+MICROBATCH], dtype=torch.long)
            frame = data["frame"].index_select(indices)
            logits = model.value_batch(frame, data["hands"].index_select(0, indices))
            target = data["targets"].index_select(0, indices).to(device)
            loss = soft_loss(logits, target)
            (loss * len(indices) / len(batch)).backward()
            total += float(loss.detach()) * len(indices)
            samples += len(indices)
        if any(p.grad is not None for p in model.policy_head.parameters()):
            raise ValueError("policy head received a value gradient")
        if first_gradients is None:
            first_gradients = {prefix: sum(float(p.grad.detach().abs().sum()) for name,p in model.named_parameters()
                if name.startswith(prefix+'.') and p.grad is not None)
                for prefix in ("encoder", "hand_projection", "trunk", "value_head")}
            if any(value <= 0 or not torch.isfinite(torch.tensor(value)) for value in first_gradients.values()):
                raise ValueError("missing or nonfinite gradient in an unfrozen module")
        norm = torch.nn.utils.clip_grad_norm_(parameters, 1.)
        if not bool(torch.isfinite(norm)):
            raise ValueError("nonfinite shared-network gradient norm")
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        steps += 1
    return dict(epoch=epoch, samples=samples, steps=steps, soft_ce=total/samples,
        ordering_sha=semantic_digest(order), first_batch_gradient_l1=first_gradients)


def _save(path, model, optimizer, identity, state):
    device = next(model.parameters()).device
    state = dict(state, cuda_rng=torch.cuda.get_rng_state(device).cpu() if device.type == "cuda" else None)
    return save_checkpoint(path, model, optimizer, identity=identity,
        optimizer_steps=state["steps"], run_state=state)


def _load(path, model, optimizer, identity, device):
    result = load_checkpoint(path, model, optimizer, expected_identity=identity, device=device)
    if result["run_state"].get("cuda_rng") is not None:
        torch.cuda.set_rng_state(result["run_state"]["cuda_rng"], device=device)
    return result["run_state"]


def restore_parent(path, checksum, model, optimizer, *, device, dataset_sha):
    if file_sha(path) != checksum:
        raise ValueError("parent checkpoint checksum differs")
    envelope = torch.load(path, weights_only=True, map_location="cpu")
    payload = envelope["payload"]
    identity = payload["identity"]
    if (identity.get("protocol") not in ("sspi-tower-shared-fit.v1", RUN_SCHEMA)
            or identity.get("dataset_sha") == dataset_sha
            or dataset_sha in payload["run_state"].get("consumed_datasets", ())
            or identity.get("seed") != SEED or identity.get("policy_frozen") is not True
            or identity.get("shared_frozen") is not False):
        raise ValueError("parent protocol differs or this dataset was already consumed")
    state = _load(path, model, optimizer, identity, device)
    if any(g["lr"] != LR or g["weight_decay"] != 1e-4 for g in optimizer.param_groups):
        raise ValueError("parent optimizer settings differ")
    return dict(file_sha=checksum, identity=identity, model_sha=semantic_digest(model.state_dict()),
        optimizer_sha=semantic_digest(optimizer.state_dict()), steps=state["steps"],
        consumed_datasets=list(dict.fromkeys([*state.get("consumed_datasets", []), identity["dataset_sha"]])))


def batch_has_work(state):
    return state["epoch"] < MAX_EPOCHS


def run(data_dir, run_dir, run_id, *, device="cuda:0", parent_checkpoint=None, parent_sha=None):
    data_dir, run_dir = Path(data_dir), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    torch.manual_seed(SEED)
    random.seed(SEED)
    torch.use_deterministic_algorithms(True)
    if device.startswith("cuda"):
        torch.cuda.manual_seed_all(SEED)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
    started = time.time()
    data, probes, manifest = load_data(data_dir)
    initial = torch.load(data_dir / "initial.pt", weights_only=True, map_location="cpu")
    model = initialize_fresh_model(SEED)
    if initial["architecture"] != model.architecture:
        raise ValueError("random initial architecture differs")
    model.load_state_dict(initial["model"], strict=True)
    if semantic_digest(model.state_dict()) != manifest["identity"]["initial_model_sha"]:
        raise ValueError("the exact original random weights were not restored")
    model.to(device)
    optimizer = create_optimizer(model)
    original_policy_sha = policy_head_sha(model)
    dataset_sha = file_sha(data_dir / "dataset.json")
    if (parent_checkpoint is None) != (parent_sha is None):
        raise ValueError("parent checkpoint and explicit SHA must be supplied together")
    parent = (restore_parent(parent_checkpoint, parent_sha, model, optimizer,
        device=device, dataset_sha=dataset_sha) if parent_checkpoint else None)
    if parent and (parent["identity"].get("initial_model_sha") != manifest["identity"]["initial_model_sha"]
            or policy_head_sha(model) != original_policy_sha):
        raise ValueError("parent random origin or frozen policy head differs")
    parent_steps = parent["steps"] if parent else 0
    initial_shared = policy_function_sha(model)
    initial_value = semantic_digest(model.value_head.state_dict())
    policy_sha = policy_head_sha(model)
    buffers_sha = semantic_digest(dict(model.named_buffers()))
    identity = dict(protocol=RUN_SCHEMA, run_id=run_id, source_sha=source_sha(),
        dataset_sha=dataset_sha, initial_model_sha=manifest["identity"]["initial_model_sha"],
        seed=SEED, batch=BATCH, microbatch=MICROBATCH, lr=LR, weight_decay=1e-4,
        policy_frozen=True, shared_frozen=False, torch=str(torch.__version__), device=device,
        gpu=torch.cuda.get_device_name() if device.startswith("cuda") else None,
        signal_streak=SIGNAL_STREAK, max_epochs=MAX_EPOCHS, original_holdout_used=False,
        parent_checkpoint=parent, starting_model_sha=semantic_digest(model.state_dict()))
    config_path = run_dir / "run_config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != identity:
        raise ValueError("existing shared-fit run identity differs")
    _atomic_json(config_path, identity)
    report_path = run_dir / "result.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if report["identity"] != identity or file_sha(run_dir / "best.pt") != report["checkpoint_sha"]:
            raise ValueError("completed shared-fit result identity differs")
        return report
    latest = run_dir / "latest.pt"
    def status(stage, **record):
        value = dict(status="running", stage=stage, **record)
        _atomic_json(run_dir / "status.json", value)
        print(json.dumps(value, allow_nan=False), flush=True)
    if latest.exists():
        state = _load(latest, model, optimizer, identity, device)
    else:
        status("baseline", train_samples=len(data["training"]["records"]), validation_samples=len(data["validation"]["records"]))
        baseline = {s:metrics(model,d) for s,d in data.items()}
        mean = sum(r["score"] for r in data["training"]["records"])/len(data["training"]["records"])
        constants = {s: min(score_metrics(d["records"],[v]*len(d["records"]),replicates=0)["mse"]
            for v in (0., mean)) for s,d in data.items()}
        state = dict(epoch=0, steps=parent_steps, baseline=baseline, constants=constants,
            best_epoch=0, best_mse=baseline["validation"]["mse"], best_metrics=baseline,
            streak=0, initial_policy=policy_probe(model,probes), initial_shared_sha=initial_shared,
            initial_value_sha=initial_value, policy_sha=policy_sha, buffers_sha=buffers_sha,
            consumed_datasets=[*(parent["consumed_datasets"] if parent else []),dataset_sha])
        _save(run_dir / "best.pt", model, optimizer, identity, state)
        _save(latest, model, optimizer, identity, state)
    while batch_has_work(state):
        if (run_dir / "STOP_AFTER_EPOCH").exists():
            _atomic_json(run_dir / "status.json", dict(status="paused_user", epoch=state["epoch"],
                latest_checkpoint_sha=file_sha(latest)))
            return None
        epoch = state["epoch"] + 1
        status("training", epoch=epoch, completed_steps=state["steps"])
        epoch_start = time.perf_counter()
        update = train_epoch(model, optimizer, data["training"], epoch, device=device)
        evaluated = {s:metrics(model,d) for s,d in data.items()}
        if policy_head_sha(model) != policy_sha or semantic_digest(dict(model.named_buffers())) != buffers_sha:
            raise ValueError("frozen policy parameters or static buffers changed")
        shared_changed = policy_function_sha(model) != initial_shared
        value_changed = semantic_digest(model.value_head.state_dict()) != initial_value
        if not shared_changed or not value_changed:
            raise ValueError("an unfrozen parameter group did not update")
        signal = fit_signal(evaluated["training"], evaluated["validation"], state["constants"])
        state.update(epoch=epoch, steps=state["steps"]+update["steps"],
            streak=state["streak"]+1 if signal else 0)
        if evaluated["validation"]["mse"] < state["best_mse"] - 1e-6:
            state.update(best_epoch=epoch, best_mse=evaluated["validation"]["mse"], best_metrics=evaluated)
            _save(run_dir / "best.pt", model, optimizer, identity, state)
        record = dict(**update, metrics=evaluated, fit_signal=signal, consecutive_signal_epochs=state["streak"],
            best_epoch=state["best_epoch"], seconds=time.perf_counter()-epoch_start,
            shared_changed=shared_changed, value_changed=value_changed, policy_head_unchanged=True,
            buffers_unchanged=True, peak_cuda_bytes=torch.cuda.max_memory_allocated() if device.startswith("cuda") else None)
        _atomic_json(run_dir / f"epoch-{epoch:04d}.json", record)
        _save(latest, model, optimizer, identity, state)
        status("epoch_complete", epoch=epoch, training_mse=evaluated["training"]["mse"],
            validation_mse=evaluated["validation"]["mse"], validation_mae=evaluated["validation"]["mae"],
            validation_direction=evaluated["validation"]["direction_accuracy"],
            signal_streak=state["streak"], seconds=record["seconds"])
    attempted_steps, completed_epochs = state["steps"], state["epoch"]
    observed = fitting_complete(state)
    state = _load(run_dir / "best.pt", model, optimizer, identity, device)
    current_policy = policy_probe(model, probes)
    reference = initialize_fresh_model(SEED, device=device)
    reference_optimizer = create_optimizer(reference)
    _load(run_dir / "best.pt", reference, reference_optimizer, identity, device)
    roundtrip = semantic_digest((model.state_dict(),optimizer.state_dict())) == semantic_digest((reference.state_dict(),reference_optimizer.state_dict()))
    if not roundtrip:
        raise ValueError("shared-fit checkpoint model/optimizer restoration differs")
    final_metrics = {s:score_metrics(d["records"], predict(model,d)) for s,d in data.items()}
    mean = sum(r["score"] for r in data["training"]["records"])/len(data["training"]["records"])
    constant_metrics = {s:{name:score_metrics(d["records"],[value]*len(d["records"]))
        for name,value in (("zero",0.),("training_mean",mean))} for s,d in data.items()}
    report = dict(schema=RUN_SCHEMA, identity=identity,
        status="fitting_observed" if observed else "batch_budget_complete", independent_acceptance=False,
        epochs=completed_epochs, attempted_steps=attempted_steps, best_epoch=state["best_epoch"],
        batch_update_steps=attempted_steps-parent_steps, parent_steps=parent_steps,
        consumed_datasets=state["consumed_datasets"],
        selected_steps=state["steps"], metrics=final_metrics, baseline=state["baseline"],
        constant_metrics=constant_metrics, validation_reused_for_selection=True,
        constants=state["constants"], checkpoint_sha=file_sha(run_dir / "best.pt"),
        model_sha=semantic_digest(model.state_dict()), optimizer_sha=semantic_digest(optimizer.state_dict()),
        shared_changed=policy_function_sha(model)!=initial_shared,
        value_changed=semantic_digest(model.value_head.state_dict())!=initial_value,
        policy_head_unchanged=policy_head_sha(model)==policy_sha, checkpoint_roundtrip=roundtrip,
        buffers_unchanged=semantic_digest(dict(model.named_buffers()))==buffers_sha,
        initial_policy_modes=state["initial_policy"], final_policy_modes=current_policy,
        max_policy_mode_tv=max((sum(abs(a-b) for a,b in zip(x,y))/2
            for x,y in zip(state["initial_policy"],current_policy)), default=None),
        elapsed_seconds=time.time()-started, new_native_games=0, original_holdout_evaluations=0)
    _atomic_json(report_path, report)
    _atomic_json(run_dir / "status.json", dict(status="complete", stage=report["status"], best_epoch=state["best_epoch"],
        validation_mse=report["metrics"]["validation"]["mse"], checkpoint_sha=report["checkpoint_sha"]))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    prep = subs.add_parser("prepare")
    prep.add_argument("--source-run", type=Path, required=True)
    prep.add_argument("--destination", type=Path, required=True)
    train = subs.add_parser("run")
    train.add_argument("--data-dir", type=Path, required=True)
    train.add_argument("--run-dir", type=Path, required=True)
    train.add_argument("--run-id", required=True)
    train.add_argument("--device", default="cuda:0")
    train.add_argument("--parent-checkpoint", type=Path)
    train.add_argument("--parent-sha")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "prepare":
        prepare(**args)
        return
    args["run_dir"].mkdir(parents=True, exist_ok=True)
    with (args["run_dir"] / "trainer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            run(**args)
        except BaseException as error:
            _atomic_json(args["run_dir"] / "status.json", dict(status="paused_failure",
                error=type(error).__name__, reason=str(error), traceback=traceback.format_exc()))
            raise


if __name__ == "__main__":
    main()
