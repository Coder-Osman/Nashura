"""Offline owner-contract evaluation; --samples counts shards, not decisions.

Strict v2 checkpoint/cache semantics by default. --initialization-only evaluates
legacy weights under NEW semantics, never reproduces the old inference contract.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch
import torch.nn.functional as F

from native_runner.training.event_cache import load_event_cache_shard, validate_event_supervised_contract
from native_runner.training.event_checkpoint import (
    INITIALIZATION_KEY, EVENT_INPUT_SEMANTICS, EVENT_ACTION_SEMANTICS, load_event_checkpoint,
)
from native_runner.training.event_model import build_event_targets
from tools.eval_event_pretrain import shard_batch


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class OwnerMetrics:
    """Mergeable sufficient counts and unsmoothed CE sums; no batch averaging.

    Card/delay/joint accuracy denominators include EVERY ground-truth ACT,
    including unalignable card labels. Cell metrics skip invalid cell fields.
    Joint requires an eligible cell; skipped cells cannot count as successes.
    Empty denominators produce null, not a misleading zero accuracy.
    """

    def __init__(self, board_width):
        if board_width <= 0:
            raise ValueError("board_width must be positive")
        self.width = board_width
        self.groups = {name: {"counts": {}, "ce": {}} for name in ("combined", "owner0", "owner1")}

    def update(self, output, targets, owners):
        owners = torch.as_tensor(owners, device=targets.gate.device)
        if owners.shape != targets.gate.shape or not bool(((owners == 0) | (owners == 1)).all()):
            raise ValueError("one owner 0/1 is required for each target")
        act = targets.gate == 1  # NOT act_mask, which drops missing card labels.
        card_valid = act & (targets.card_row >= 0) & (targets.card_row < output.card_logits.shape[-1])
        cell_valid = act & targets.cell_mask & (targets.cell >= 0) & (targets.cell < output.cell_logits.shape[-1])
        delay_valid = act & (targets.delay_bin >= 0) & (targets.delay_bin < output.delay_logits.shape[-1])
        gate = output.gate_logits.argmax(-1)
        card = output.card_logits.argmax(-1)
        cell = output.cell_logits.argmax(-1)
        delay = output.delay_logits.argmax(-1)
        card_ok = card_valid & (card == targets.card_row)
        cell_near = cell_valid & ((cell % self.width - targets.cell % self.width).abs() <= 1) & ((cell // self.width - targets.cell // self.width).abs() <= 1)
        delay_near = delay_valid & ((delay - targets.delay_bin).abs() <= 1)
        top3 = output.card_logits.topk(min(3, output.card_logits.shape[-1]), -1).indices
        flags = {
            "rows": torch.ones_like(act), "act_total": act,
            "wait_total": ~act, "predicted_act": gate == 1,
            "gate_correct": gate == targets.gate,
            "act_gate_correct": act & (gate == 1), "wait_gate_correct": ~act & (gate == 0),
            "card_valid": card_valid, "card_invalid": act & ~card_valid,
            "card_correct": card_ok,
            "card_top3": card_valid & (top3 == targets.card_row[:, None]).any(-1),
            "delay_valid": delay_valid, "delay_exact": delay_valid & (delay == targets.delay_bin),
            "delay_within1": delay_near,
            "delay_within3": delay_valid & ((delay - targets.delay_bin).abs() <= 3),
            "cell_total": cell_valid, "cell_invalid": act & ~cell_valid,
            "cell_exact": cell_valid & (cell == targets.cell), "cell_within1": cell_near,
            "joint_correct": act & (gate == targets.gate) & (gate == 1) & card_ok & cell_near & delay_near,
        }
        ce_inputs = {
            "gate": (output.gate_logits, targets.gate, flags["rows"]),
            "card": (output.card_logits, targets.card_row, card_valid),
            "cell": (output.cell_logits, targets.cell, cell_valid),
            "delay": (output.delay_logits, targets.delay_bin, delay_valid),
        }
        for name, group in self.groups.items():
            selected = flags["rows"] if name == "combined" else owners == int(name[-1])
            for key, flag in flags.items():
                group["counts"][key] = group["counts"].get(key, 0) + int((flag & selected).sum().item())
            for key, (logits, label, valid) in ce_inputs.items():
                mask = valid & selected
                count = int(mask.sum().item())
                total = float(F.cross_entropy(logits[mask].float(), label[mask], reduction="none").double().sum().item()) if count else 0.0
                stats = group["ce"].setdefault(key, {"sum": 0.0, "count": 0})
                stats["sum"] += total
                stats["count"] += count

    def report(self):
        denominators = {
            "gate_correct": "rows", "act_gate_correct": "act_total", "wait_gate_correct": "wait_total",
            "card_correct": "act_total", "card_top3": "act_total",
            "delay_exact": "act_total", "delay_within1": "act_total", "delay_within3": "act_total",
            "cell_exact": "cell_total", "cell_within1": "cell_total", "joint_correct": "act_total",
        }
        result = {}
        for name, group in self.groups.items():
            counts = dict(group["counts"])
            result[name] = {
                "counts": counts,
                "accuracy": {key: counts.get(key, 0) / counts[den] if counts.get(den) else None for key, den in denominators.items()},
                "ce": {key: {**value, "mean": value["sum"] / value["count"] if value["count"] else None} for key, value in group["ce"].items()},
            }
        return result


def validate_inventory(cache_root):
    """Load and checksum EVERY shard before selection; never silently skip one."""
    paths = sorted(cache_root.glob("*-shard-*.json"))
    if not paths:
        raise ValueError("no heldout shard summaries found")
    inventory = []
    for path in paths:
        raw = path.read_bytes()
        shard = load_event_cache_shard(path, verify=True)
        validate_event_supervised_contract(shard.manifest.get("contract"))
        if not shard.frames or shard.frames != shard.summary.get("frames") or shard.frames != shard.manifest.get("frames"):
            raise ValueError(f"invalid frame counts: {path}")
        inventory.append({
            "summary_file": path.name, "summary_sha256": hashlib.sha256(raw).hexdigest(),
            "data_file": shard.summary["data_file"], "compressed_sha256": shard.summary["compressed_sha256"],
            "frames": shard.frames, "contract_sha256": digest(shard.manifest["contract"]),
            "owner_rows": {str(owner): sum(r.owner == owner for r in shard.records) for owner in (0, 1)},
        })
    return inventory


def atomic_report(path, report):
    path = Path(path)
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def evaluate(args):
    root = args.cache_root.resolve()
    inventory = validate_inventory(root)
    selected = list(inventory)
    random.Random(args.seed).shuffle(selected)
    selected = selected[:args.samples]
    torch.manual_seed(args.seed)
    loaded = load_event_checkpoint(
        args.checkpoint, device=args.device, initialization_only=args.initialization_only,
        legacy_snapshot_only=args.legacy_snapshot_only, legacy_model_scale=args.legacy_model_scale,
    )
    model = loaded.model.eval()
    config = model.event_config
    metrics = {mode: OwnerMetrics(config.board_width) for mode in ("teacher_forced", "free_running")}
    with torch.inference_mode():
        for start in range(0, len(selected), args.batch_shards):
            shards = []
            for item in selected[start:start + args.batch_shards]:
                path = root / item["summary_file"]
                if hashlib.sha256(path.read_bytes()).hexdigest() != item["summary_sha256"]:
                    raise ValueError(f"heldout summary changed during evaluation: {path}")
                shard = load_event_cache_shard(path, verify=True)
                validate_event_supervised_contract(shard.manifest.get("contract"))
                shards.append(shard)
            observations, records = shard_batch(shards)
            for offset in range(0, len(records), args.chunk_decisions):
                chunk = records[offset:offset + args.chunk_decisions]
                batch = observations.narrow_batch(offset, len(chunk)).to_model_input(args.device, float_dtype=torch.float32)
                targets = build_event_targets(batch, chunk, model.backbone.catalog, event_config=config)
                context = model.encode(batch, model.initial_state(len(chunk), device=args.device),
                                       episode_start=torch.ones(len(chunk), dtype=torch.bool, device=args.device))
                owners = [record.owner for record in chunk]
                teacher_cell = torch.where(targets.cell_mask, targets.cell, torch.zeros_like(targets.cell))
                output = model.heads(context, card_row=targets.card_row, cell=teacher_cell)
                metrics["teacher_forced"].update(output, targets, owners)
                output = model.heads(context, card_row=None, cell=None)
                metrics["free_running"].update(output, targets, owners)
    # Fail rather than publish metrics for an inventory that changed mid-run.
    if sorted(p.name for p in root.glob("*-shard-*.json")) != [i["summary_file"] for i in inventory]:
        raise ValueError("heldout inventory changed during evaluation")
    for item in inventory:
        if hashlib.sha256((root / item["summary_file"]).read_bytes()).hexdigest() != item["summary_sha256"]:
            raise ValueError("heldout summary changed during evaluation")
    return {
        "schema": "event-owner-contract-evaluation.v1",
        "checkpoint": str(args.checkpoint.resolve()), "checkpoint_sha256": loaded.checkpoint_sha256,
        "config_contract_sha256": loaded.contract_sha256,
        "initialization_only": args.initialization_only,
        "initialization_provenance": loaded.payload.get(INITIALIZATION_KEY),
        "input_semantics": EVENT_INPUT_SEMANTICS, "action_semantics": EVENT_ACTION_SEMANTICS,
        "snapshot_only": model.backbone.config.snapshot_only,
        "configs": {"backbone": asdict(model.backbone.config), "model": asdict(model.config), "event": asdict(config)},
        "heldout": {"cache_root": str(root), "inventory": inventory, "inventory_sha256": digest(inventory),
                    "selected": selected, "selected_sha256": digest(selected), "seed": args.seed,
                    "requested_shards": args.samples, "selected_shards": len(selected)},
        "execution": {"device": args.device, "batch_shards": args.batch_shards, "chunk_decisions": args.chunk_decisions,
                      "torch_version": str(torch.__version__), "state": "fresh state and episode_start=True per decision"},
        "metric_contract": {
            "teacher_forced": "true card and true cell condition downstream heads; invalid cell uses index 0 for conditioning",
            "free_running": "raw argmax card -> cell -> delay; no engine or candidate legality masks; not legal-action success",
            "denominators": "gate: all rows; card/delay/joint: all ground-truth ACT; cell: valid target cells only",
            "joint": "ground-truth ACT, correct gate predicting ACT, correct card, Chebyshev cell distance <=1, delay-bin distance <=1; invalid cell fails joint",
            "loss": "unsmoothed per-head cross entropy sums/counts and head means; no batch-mean or composite training loss",
            "missing_card": "unalignable cards fail card/joint accuracy and skip card CE; teacher conditioning uses wait embedding",
        },
        "metrics": {mode: value.report() for mode, value in metrics.items()},
    }


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True, help="explicit fixed heldout cache, never training discovery")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=positive, default=256, help="number of shards (all if fewer exist)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--batch-shards", type=positive, default=1)
    parser.add_argument("--chunk-decisions", type=positive, default=64)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--initialization-only", action="store_true", help="explicit old-IL baseline under NEW owner-neutral semantics")
    parser.add_argument("--legacy-snapshot-only", choices=("true", "false"))
    parser.add_argument("--legacy-model-scale", type=float)
    args = parser.parse_args(argv)
    if not args.initialization_only and (args.legacy_snapshot_only is not None or args.legacy_model_scale is not None):
        parser.error("legacy overrides require --initialization-only")
    report = evaluate(args)
    atomic_report(args.output, report)
    print(json.dumps({"output": str(args.output.resolve()), "selected_sha256": report["heldout"]["selected_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
