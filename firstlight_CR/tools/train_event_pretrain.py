"""Supervised pretraining for the event-driven card policy.

Two modes:

* one-shot: train ``--epochs`` over the shards listed by ``--cache-root/index.json``;
* streaming (``--watch-seconds > 0``): rescan the cache root for finished shards,
  train only the shards not yet consumed, checkpoint after each round, and sleep.
  This lets capture machines feed the trainer continuously: a shard is trained
  as soon as its summary file appears and stops being modified.

DDP: launch with ``torchrun --nproc_per_node=N``; ranks discover the same shard
files and truncate to the minimum count seen by any rank, so a shard arriving
mid-scan cannot desynchronize them.

Usage:
    .venv/bin/python tools/train_event_pretrain.py --cache-root RUNS/event-obs \
        --output-dir RUNS/event-pretrain --epochs 3
    .venv/bin/torchrun --nproc_per_node=8 tools/train_event_pretrain.py \
        --cache-root RUNS/event-obs --output-dir RUNS/event-pretrain \
        --watch-seconds 120 --min-new-shards 200
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

from native_runner.training.event_cache import load_event_cache_shard, validate_event_supervised_contract  # noqa: E402
from native_runner.training.event_config import EventPolicyConfigV1  # noqa: E402
from native_runner.training.event_model import (  # noqa: E402
    EventCardPolicyV1,
    EventModelConfigV1,
    EventPolicyOutputV1,
    build_event_model_v1,
    build_event_targets,
    event_il_loss,
)
from native_runner.training.v4.cache import _pad_dynamic_observation  # noqa: E402
from native_runner.training.v4.tensors import concatenate_tensor_records  # noqa: E402


class _EventTrainModule(torch.nn.Module):
    """Return plain tensors so DDP can traverse the forward outputs."""

    def __init__(self, policy: EventCardPolicyV1) -> None:
        super().__init__()
        self.policy = policy

    def forward(self, observations, state, *, episode_start=None, card_row=None, cell=None):
        output = self.policy(observations, state, episode_start=episode_start, card_row=card_row, cell=cell)
        return output.gate_logits, output.card_logits, output.delay_logits, output.cell_logits

    def initial_state(self, *args, **kwargs):
        return self.policy.initial_state(*args, **kwargs)


def _is_validation_shard(name: str, *, fraction: float, seed: int) -> bool:
    """Deterministic, restart-stable hold-out split by summary filename."""

    if fraction <= 0.0:
        return False
    digest = hashlib.sha1(f"{seed}:{name}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 10000
    return bucket < int(fraction * 10000)


def evaluate_policy(
    policy,
    summaries: list[dict[str, object]],
    *,
    cache_root: Path,
    catalog,
    event_config: EventPolicyConfigV1,
    device: str,
    batch_shards: int,
    delay_neighbor_weight: float,
    delay_coarse_weight: float = 0.0,
    delay_coarse_group: int = 5,
) -> dict[str, object]:
    """Per-head loss and top-1 accuracy on a set of shards (teacher-forced card)."""

    batches = [
        summaries[start : start + batch_shards] for start in range(0, len(summaries), batch_shards)
    ]
    totals = {
        "rows": 0, "gate_total": 0, "gate_correct": 0,
        "act_total": 0, "card_correct": 0, "card_top3": 0,
        "delay_correct": 0, "delay_within1": 0, "delay_within3": 0, "delay_coarse_correct": 0,
        "cell_total": 0, "cell_correct": 0, "cell_within1": 0,
    }
    loss_sum = 0.0
    width = event_config.board_width
    was_training = policy.training
    policy.eval()
    with torch.no_grad():
        for summaries_batch in batches:
            shards = [
                load_event_cache_shard(cache_root / str(item["summary_file"]), verify=False)
                for item in summaries_batch
            ]
            observations, records = _shard_batch(shards)
            observations = observations.to_model_input(device, float_dtype=torch.float32)
            targets = build_event_targets(observations, records, catalog, event_config=event_config)
            state = policy.initial_state(observations.batch_size, device=device)
            episode_start = torch.ones(observations.batch_size, dtype=torch.bool, device=device)
            output = policy(
                observations, state, episode_start=episode_start, card_row=targets.card_row,
                cell=targets.cell.clamp_min(0),
            )
            loss, _parts = event_il_loss(
                output,
                targets,
                event_config=event_config,
                delay_neighbor_weight=delay_neighbor_weight,
                delay_coarse_weight=delay_coarse_weight,
                delay_coarse_group=delay_coarse_group,
            )
            loss_sum += float(loss)
            gate_pred = output.gate_logits.argmax(dim=-1)
            rows = int(targets.gate.numel())
            totals["rows"] += rows
            totals["gate_total"] += rows
            totals["gate_correct"] += int((gate_pred == targets.gate).sum().item())
            act = targets.act_mask
            if bool(act.any()):
                card_logits = output.card_logits[act]
                card_target = targets.card_row[act]
                card_pred = card_logits.argmax(dim=-1)
                totals["act_total"] += int(card_target.numel())
                totals["card_correct"] += int((card_pred == card_target).sum().item())
                top3 = card_logits.topk(min(3, card_logits.shape[-1]), dim=-1).indices
                totals["card_top3"] += int((top3 == card_target[:, None]).any(dim=-1).sum().item())
                delay_pred = output.delay_logits[act].argmax(dim=-1)
                delay_target = targets.delay_bin[act]
                totals["delay_correct"] += int((delay_pred == delay_target).sum().item())
                totals["delay_within1"] += int(((delay_pred - delay_target).abs() <= 1).sum().item())
                totals["delay_within3"] += int(((delay_pred - delay_target).abs() <= 3).sum().item())
                totals["delay_coarse_correct"] += int(
                    ((delay_pred // delay_coarse_group) == (delay_target // delay_coarse_group)).sum().item()
                )
                cell_sel = targets.cell_mask[act]
                if bool(cell_sel.any()):
                    cell_pred = output.cell_logits[act][cell_sel].argmax(dim=-1)
                    cell_target = targets.cell[act][cell_sel]
                    totals["cell_total"] += int(cell_target.numel())
                    totals["cell_correct"] += int((cell_pred == cell_target).sum().item())
                    dx = (cell_pred % width) - (cell_target % width)
                    dy = (cell_pred // width) - (cell_target // width)
                    totals["cell_within1"] += int(((dx.abs() <= 1) & (dy.abs() <= 1)).sum().item())
    if was_training:
        policy.train()

    def ratio(num: int, den: int) -> float:
        return round(num / den, 6) if den else 0.0

    return {
        "batch_count": len(batches),
        "rows": totals["rows"],
        "loss": round(loss_sum / max(1, len(batches)), 6),
        "gate_acc": ratio(totals["gate_correct"], totals["gate_total"]),
        "card_top1": ratio(totals["card_correct"], totals["act_total"]),
        "card_top3": ratio(totals["card_top3"], totals["act_total"]),
        "delay_exact": ratio(totals["delay_correct"], totals["act_total"]),
        "delay_within1": ratio(totals["delay_within1"], totals["act_total"]),
        "delay_within3": ratio(totals["delay_within3"], totals["act_total"]),
        "delay_coarse_acc": ratio(totals["delay_coarse_correct"], totals["act_total"]),
        "cell_exact": ratio(totals["cell_correct"], totals["cell_total"]),
        "cell_within1_tile": ratio(totals["cell_within1"], totals["cell_total"]),
    }


def _shard_batch(shards):
    for shard in shards:
        validate_event_supervised_contract(shard.manifest.get("contract"))
    active_capacity = max(int(shard.observations_batch().active_effects.mask.shape[1]) for shard in shards)
    relation_capacity = max(int(shard.observations_batch().relation_edges.mask.shape[1]) for shard in shards)
    candidate_capacity = max(int(shard.observations_batch().candidates.mask.shape[1]) for shard in shards)
    frames = tuple(
        _pad_dynamic_observation(
            shard.observations_batch(),
            active_effect_count=active_capacity,
            relation_edge_count=relation_capacity,
            candidate_count=candidate_capacity,
        )
        for shard in shards
    )
    records = [record for shard in shards for record in shard.records]
    return concatenate_tensor_records(frames), records


def _discover_shards(cache_root: Path, *, min_age_seconds: float) -> list[dict[str, object]]:
    """List finished shard summaries that have stopped being written."""

    now = time.time()
    found: list[tuple[str, dict[str, object]]] = []
    for summary_path in cache_root.glob("*-shard-*.json"):
        try:
            if now - summary_path.stat().st_mtime < min_age_seconds:
                continue
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(summary, dict) or not summary.get("frames"):
            continue
        data_name = summary.get("data_file")
        if not isinstance(data_name, str) or not (summary_path.parent / data_name).is_file():
            continue
        summary.setdefault("summary_file", summary_path.name)
        found.append((summary_path.name, summary))
    found.sort(key=lambda item: item[0])
    return [summary for _name, summary in found]


def _rank_consistent_shards(
    summaries: list[dict[str, object]], *, distributed: bool, rank: int, device: str
) -> list[dict[str, object]]:
    if not distributed:
        return summaries
    import torch.distributed as dist

    local = torch.tensor([len(summaries)], dtype=torch.long, device=device)
    dist.all_reduce(local, op=dist.ReduceOp.MIN)
    limit = int(local.item())
    if len(summaries) != limit and rank == 0:
        print(
            json.dumps({"event": "shard-count-skew", "local": len(summaries), "using": limit}),
            flush=True,
        )
    return summaries[:limit]


def _run_epochs(
    *,
    model,
    distributed: bool,
    rank: int,
    sampler,
    optimizer,
    summaries: list[dict[str, object]],
    cache_root: Path,
    event_config: EventPolicyConfigV1,
    delay_neighbor_weight: float,
    delay_coarse_weight: float,
    delay_coarse_group: int,
    epochs: int,
    device: str,
    batch_shards: int,
    grad_clip: float,
    log_every: int,
    step: int,
) -> int:
    batches = [
        summaries[start : start + batch_shards] for start in range(0, len(summaries), batch_shards)
    ]
    catalog = (model.module if distributed else model).policy.backbone.catalog
    last_epoch_loss = 0.0
    for epoch in range(epochs):
        if sampler is not None:
            sampler.set_epoch(epoch)
        order = list(sampler) if sampler is not None else list(range(len(batches)))
        running = {
            "loss": 0.0, "gate": 0.0, "card": 0.0, "delay": 0.0, "delay_coarse": 0.0,
            "cell": 0.0, "rows": 0, "acts": 0,
        }
        for batch_index in order:
            summaries_batch = batches[batch_index]
            shards = [
                load_event_cache_shard(cache_root / item["summary_file"], verify=False)
                for item in summaries_batch
            ]
            observations, records = _shard_batch(shards)
            observations = observations.to_model_input(device, float_dtype=torch.float32)
            targets = build_event_targets(observations, records, catalog, event_config=event_config)
            state = (model.module if distributed else model).initial_state(
                observations.batch_size, device=device
            )
            episode_start = torch.ones(observations.batch_size, dtype=torch.bool, device=device)
            tensors = model(
                observations, state, episode_start=episode_start, card_row=targets.card_row,
                cell=targets.cell.clamp_min(0),
            )
            output = EventPolicyOutputV1(
                gate_logits=tensors[0],
                card_logits=tensors[1],
                delay_logits=tensors[2],
                cell_logits=tensors[3],
                policy_context=None,
                context=None,
            )
            loss, parts = event_il_loss(
                output,
                targets,
                event_config=event_config,
                delay_neighbor_weight=delay_neighbor_weight,
                delay_coarse_weight=delay_coarse_weight,
                delay_coarse_group=delay_coarse_group,
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            if grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            optimizer.step()
            from native_runner.training.event_checkpoint import complete_event_initialization

            complete_event_initialization((model.module if distributed else model).policy)
            running["loss"] += float(loss.detach())
            running["gate"] += float(parts["gate"].detach())
            running["card"] += float(parts["card"].detach())
            running["delay"] += float(parts["delay"].detach())
            if "delay_coarse" in parts:
                running["delay_coarse"] += float(parts["delay_coarse"].detach())
            running["cell"] += float(parts["cell"].detach())
            running["rows"] += observations.batch_size
            running["acts"] += targets.act_count
            step += 1
            if rank == 0 and log_every > 0 and step % log_every == 0:
                print(
                    json.dumps(
                        {
                            "event": "step",
                            "step": step,
                            "loss": round(float(loss.detach()), 6),
                            "rows": observations.batch_size,
                            "acts": targets.act_count,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
        if rank == 0:
            divisor = max(1, len(order))
            print(
                json.dumps(
                    {
                        "event": "epoch",
                        "epoch": epoch,
                        "batches": len(order),
                        "rows": running["rows"],
                        "acts": running["acts"],
                        "mean_loss": round(running["loss"] / divisor, 6),
                        "mean_gate": round(running["gate"] / divisor, 6),
                        "mean_card": round(running["card"] / divisor, 6),
                        "mean_delay": round(running["delay"] / divisor, 6),
                        "mean_delay_coarse": round(running["delay_coarse"] / divisor, 6),
                        "mean_cell": round(running["cell"] / divisor, 6),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            last_epoch_loss = running["loss"] / divisor
    return step, last_epoch_loss


def _save_checkpoint(
    model, *, distributed: bool, rank: int, output_dir: Path, model_config, event_config, weight, step
) -> None:
    if rank != 0:
        return
    from native_runner.training.event_checkpoint import save_event_checkpoint

    unwrapped = (model.module if distributed else model).policy
    save_event_checkpoint(output_dir / "event-pretrain.pt", unwrapped, extra={
        "delay_neighbor_weight": weight,
        "steps": step,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-shards", type=int, default=4)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--grad-clip", type=float, default=1.0)
    parser.add_argument("--delay-neighbor-weight", type=float, default=0.1)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--log-every", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--watch-seconds", type=float, default=0.0, help="0 disables streaming mode")
    parser.add_argument("--min-new-shards", type=int, default=100)
    parser.add_argument("--min-age-seconds", type=float, default=45.0)
    parser.add_argument("--rounds", type=int, default=0, help="0 runs until interrupted")
    parser.add_argument("--consumed-file", type=Path, default=None)
    parser.add_argument("--init-checkpoint", type=Path, default=None)
    parser.add_argument("--initialize-from-legacy", action="store_true",
                        help="explicitly use old event weights as initialization under the new owner-relative contract")
    parser.add_argument("--val-fraction", type=float, default=0.05, help="held-out shard fraction (0 disables)")
    parser.add_argument("--val-seed", type=int, default=1234)
    parser.add_argument("--val-samples", type=int, default=150, help="max held-out shards per validation pass")
    parser.add_argument("--delay-coarse-weight", type=float, default=0.3, help="auxiliary coarse delay loss weight")
    parser.add_argument("--delay-coarse-group", type=int, default=5, help="fine bins per coarse delay group")
    parser.add_argument("--delay-lr-mult", type=float, default=2.0, help="learning-rate multiplier for delay-head params")
    parser.add_argument(
        "--adaptive-epochs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="lower the per-round epoch count when the train/val gap widens",
    )
    parser.add_argument("--gap-tolerance", type=float, default=0.05, help="gap growth that triggers one epoch anneal step")
    parser.add_argument(
        "--early-stop-patience",
        type=int,
        default=0,
        help="stop after this many consecutive validation rounds without improvement (0 disables)",
    )
    args = parser.parse_args()
    if args.initialize_from_legacy and args.init_checkpoint is None:
        parser.error("--initialize-from-legacy requires --init-checkpoint")

    if args.epochs <= 0 or args.batch_shards <= 0 or args.lr <= 0.0:
        raise ValueError("event pretraining arguments are invalid")
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    distributed = world_size > 1
    if distributed:
        import torch.distributed as dist

        dist.init_process_group(backend="nccl")
        torch.cuda.set_device(local_rank)
        device = f"cuda:{local_rank}"
    else:
        device = args.device
    torch.manual_seed(args.seed + rank)

    cache_root = args.cache_root.resolve()
    output_dir = args.output_dir.resolve()
    if rank == 0:
        output_dir.mkdir(parents=True, exist_ok=True)
    if distributed:
        import torch.distributed as dist

        dist.barrier()
    consumed_path = args.consumed_file or (output_dir / "consumed-shards.json")
    streaming = args.watch_seconds > 0.0
    if not streaming and not (cache_root / "index.json").is_file():
        raise FileNotFoundError(f"missing cache index: {cache_root / 'index.json'}")

    if args.init_checkpoint is not None:
        from native_runner.training.event_checkpoint import load_event_checkpoint

        loaded = load_event_checkpoint(args.init_checkpoint, device=device,
                                       initialization_only=args.initialize_from_legacy)
        policy = loaded.model
        event_config, model_config = policy.event_config, policy.config
        if rank == 0:
            print(json.dumps({"event": "init-checkpoint", "path": str(args.init_checkpoint),
                              "checkpoint_sha256": loaded.checkpoint_sha256,
                              "model_contract_hash": loaded.contract_sha256}), flush=True)
    else:
        event_config = EventPolicyConfigV1()
        model_config = EventModelConfigV1(delay_neighbor_weight=args.delay_neighbor_weight)
        policy = build_event_model_v1(device=device, model_config=model_config, event_config=event_config)
    model = _EventTrainModule(policy)
    model.train()
    sampler = None
    if distributed:
        import torch.distributed as dist
        from torch.utils.data.distributed import DistributedSampler

        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[local_rank], find_unused_parameters=True
        )
    delay_params = [param for name, param in model.named_parameters() if "delay" in name]
    other_params = [param for name, param in model.named_parameters() if "delay" not in name]
    optimizer = torch.optim.AdamW(
        [
            {"params": other_params, "lr": args.lr},
            {"params": delay_params, "lr": args.lr * args.delay_lr_mult},
        ],
        weight_decay=args.weight_decay,
    )

    step = 0
    started = time.perf_counter()
    if streaming:
        consumed: set[str] = set()
        if consumed_path.is_file():
            consumed = set(json.loads(consumed_path.read_text(encoding="utf-8")))
        round_index = 0
        no_improve_rounds = 0
        stop_training = False
        effective_epochs = args.epochs
        previous_gap: float | None = None
        while args.rounds <= 0 or round_index < args.rounds:
            discovered = _discover_shards(cache_root, min_age_seconds=args.min_age_seconds)
            discovered = _rank_consistent_shards(
                discovered, distributed=distributed, rank=rank, device=device
            )
            if args.val_fraction > 0.0:
                validation_pool = [
                    item
                    for item in discovered
                    if _is_validation_shard(
                        str(item["summary_file"]), fraction=args.val_fraction, seed=args.val_seed
                    )
                    and str(item["summary_file"]) not in consumed
                ]
                train_pool = [
                    item
                    for item in discovered
                    if not _is_validation_shard(
                        str(item["summary_file"]), fraction=args.val_fraction, seed=args.val_seed
                    )
                ]
            else:
                validation_pool = []
                train_pool = discovered
            pending = [item for item in train_pool if str(item["summary_file"]) not in consumed]
            if len(pending) < args.min_new_shards:
                if rank == 0:
                    print(
                        json.dumps(
                            {
                                "event": "waiting",
                                "available": len(discovered),
                                "pending": len(pending),
                                "min_new_shards": args.min_new_shards,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                time.sleep(args.watch_seconds)
                continue
            sampler = None
            if distributed:
                from torch.utils.data.distributed import DistributedSampler

                sampler = DistributedSampler(
                    list(range((len(pending) + args.batch_shards - 1) // args.batch_shards)), shuffle=True
                )
            step, last_train_loss = _run_epochs(
                model=model,
                distributed=distributed,
                rank=rank,
                sampler=sampler,
                optimizer=optimizer,
                summaries=pending,
                cache_root=cache_root,
                event_config=event_config,
                delay_neighbor_weight=args.delay_neighbor_weight,
                delay_coarse_weight=args.delay_coarse_weight,
                delay_coarse_group=args.delay_coarse_group,
                epochs=effective_epochs,
                device=device,
                batch_shards=args.batch_shards,
                grad_clip=args.grad_clip,
                log_every=args.log_every,
                step=step,
            )
            _save_checkpoint(
                model,
                distributed=distributed,
                rank=rank,
                output_dir=output_dir,
                model_config=model_config,
                event_config=event_config,
                weight=args.delay_neighbor_weight,
                step=step,
            )
            consumed.update(str(item["summary_file"]) for item in pending)
            if rank == 0:
                consumed_path.write_text(
                    json.dumps(sorted(consumed), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "event": "round",
                            "round": round_index,
                            "trained_shards": len(pending),
                            "consumed_total": len(consumed),
                            "steps": step,
                            "elapsed_s": round(time.perf_counter() - started, 3),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            if rank == 0 and validation_pool:
                report = evaluate_policy(
                    (model.module if distributed else model).policy,
                    validation_pool[: args.val_samples],
                    cache_root=cache_root,
                    catalog=(model.module if distributed else model).policy.backbone.catalog,
                    event_config=event_config,
                    device=device,
                    batch_shards=args.batch_shards,
                    delay_neighbor_weight=args.delay_neighbor_weight,
                    delay_coarse_weight=args.delay_coarse_weight,
                    delay_coarse_group=args.delay_coarse_group,
                )
                report["event"] = "val"
                report["round"] = round_index
                report["held_out_total"] = len(validation_pool)
                report["train_loss"] = round(float(last_train_loss), 6)
                report["train_val_gap"] = round(float(report["loss"]) - float(last_train_loss), 6)
                best_record_path = output_dir / "val-best.json"
                previous_best = None
                if best_record_path.is_file():
                    try:
                        previous_best = json.loads(best_record_path.read_text(encoding="utf-8"))
                    except ValueError:
                        previous_best = None
                improved = previous_best is None or float(report["loss"]) < float(previous_best["val_loss"]) - 1e-4
                if improved:
                    no_improve_rounds = 0
                    torch.save(
                        torch.load(output_dir / "event-pretrain.pt", map_location="cpu", weights_only=False),
                        output_dir / "event-pretrain.best.pt",
                    )
                    best_record_path.write_text(
                        json.dumps(
                            {
                                "val_loss": float(report["loss"]),
                                "round": round_index,
                                "step": step,
                                "rows": report["rows"],
                                "accuracy": {
                                    key: report[key]
                                    for key in (
                                        "gate_acc", "card_top1", "card_top3",
                                        "delay_exact", "delay_within1", "delay_coarse_acc",
                                        "cell_exact", "cell_within1_tile",
                                    )
                                },
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                else:
                    no_improve_rounds += 1
                report["best_val_loss"] = (
                    float(report["loss"]) if improved else float(previous_best["val_loss"])
                )
                report["no_improve_rounds"] = no_improve_rounds
                report["improved"] = improved
                report["effective_epochs"] = effective_epochs
                gap = float(report["train_val_gap"])
                if (
                    args.adaptive_epochs
                    and effective_epochs > 1
                    and previous_gap is not None
                    and gap > previous_gap + args.gap_tolerance
                ):
                    effective_epochs -= 1
                    print(
                        json.dumps(
                            {
                                "event": "epoch-anneal",
                                "reason": "train/val gap widened",
                                "previous_gap": round(previous_gap, 6),
                                "gap": round(gap, 6),
                                "effective_epochs": effective_epochs,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                previous_gap = gap
                print(json.dumps(report, sort_keys=True), flush=True)
                if args.early_stop_patience > 0 and no_improve_rounds >= args.early_stop_patience:
                    print(
                        json.dumps(
                            {
                                "event": "early-stop",
                                "reason": "validation loss did not improve",
                                "patience": args.early_stop_patience,
                                "best_val_loss": report["best_val_loss"],
                                "rounds": round_index + 1,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    stop_training = True
            if distributed:
                import torch.distributed as dist

                flag = torch.tensor([1 if stop_training else 0], dtype=torch.long, device=device)
                dist.broadcast(flag, src=0)
                stop_training = bool(int(flag.item()))
                dist.barrier()
            round_index += 1
            if stop_training:
                break
    else:
        index = json.loads((cache_root / "index.json").read_text(encoding="utf-8"))
        summaries = [item for item in index["shards"] if item.get("frames")]
        if args.max_batches > 0:
            summaries = summaries[: args.max_batches * args.batch_shards]
        sampler = None
        if distributed:
            from torch.utils.data.distributed import DistributedSampler

            sampler = DistributedSampler(
                list(range((len(summaries) + args.batch_shards - 1) // args.batch_shards)), shuffle=True
            )
        step, last_train_loss = _run_epochs(
            model=model,
            distributed=distributed,
            rank=rank,
            sampler=sampler,
            optimizer=optimizer,
            summaries=summaries,
            cache_root=cache_root,
            event_config=event_config,
            delay_neighbor_weight=args.delay_neighbor_weight,
            delay_coarse_weight=args.delay_coarse_weight,
            delay_coarse_group=args.delay_coarse_group,
            epochs=args.epochs,
            device=device,
            batch_shards=args.batch_shards,
            grad_clip=args.grad_clip,
            log_every=args.log_every,
            step=step,
        )
        _save_checkpoint(
            model,
            distributed=distributed,
            rank=rank,
            output_dir=output_dir,
            model_config=model_config,
            event_config=event_config,
            weight=args.delay_neighbor_weight,
            step=step,
        )

    if rank == 0:
        print(json.dumps({"ok": True, "steps": step, "output_dir": str(output_dir)}, sort_keys=True), flush=True)
    if distributed:
        import torch.distributed as dist

        dist.barrier()
        dist.destroy_process_group()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
