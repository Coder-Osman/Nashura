"""Evaluate an event-policy checkpoint: per-head accuracy and cross-entropy.

The delay and cell heads are conditioned on the teacher-forced card row, the
same conditioning used during pretraining, so the numbers describe each head
given the true card.  Reported metrics:

* gate: ACT/WAIT top-1 accuracy;
* card: deck-row top-1 and top-3 accuracy on ACT rows;
* delay: exact-bin and within-1-bin accuracy on ACT rows;
* cell: exact-cell and within-1-tile accuracy on ACT rows with a cell target;
* joint: card top-1 AND delay within 1 AND cell within 1.

Usage:
    .venv/bin/python tools/eval_event_pretrain.py \
        --checkpoint RUNS/event-pretrain/event-pretrain.pt \
        --cache-root RUNS/event-cache-live --samples 200
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

from native_runner.training.event_cache import load_event_cache_shard, validate_event_supervised_contract  # noqa: E402
from native_runner.training.event_model import (  # noqa: E402
    EventPolicyOutputV1,
    build_event_targets,
    event_il_loss,
)
from native_runner.training.v4.cache import _pad_dynamic_observation  # noqa: E402
from native_runner.training.v4.tensors import concatenate_tensor_records  # noqa: E402


def shard_batch(shards):
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


def discover(cache_root: Path) -> list[dict[str, object]]:
    found: list[tuple[str, dict[str, object]]] = []
    for summary_path in cache_root.glob("*-shard-*.json"):
        try:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--batch-shards", type=int, default=8)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--delay-coarse-group", type=int, default=5)
    args = parser.parse_args()

    from native_runner.training.event_checkpoint import load_event_checkpoint

    loaded = load_event_checkpoint(args.checkpoint, device=args.device)
    model, state = loaded.model, loaded.payload
    event_config = model.event_config
    model.eval()
    catalog = model.backbone.catalog

    summaries = discover(args.cache_root.resolve())
    if not summaries:
        raise ValueError("no shards found")
    random.Random(args.seed).shuffle(summaries)
    selected = summaries[: args.samples]
    batches = [
        selected[start : start + args.batch_shards]
        for start in range(0, len(selected), args.batch_shards)
    ]

    counts = {key: 0 for key in (
        "rows", "gate_total", "gate_correct",
        "act_total", "card_correct", "card_top3", "delay_correct", "delay_within1",
        "joint_correct", "cell_total", "cell_correct", "cell_within1",
        "delay_within3", "delay_coarse_correct",
    )}
    loss_sums = {"loss": 0.0, "gate": 0.0, "card": 0.0, "delay": 0.0, "cell": 0.0}
    width = event_config.board_width
    with torch.no_grad():
        for summaries_batch in batches:
            shards = [
                load_event_cache_shard(args.cache_root.resolve() / str(item["summary_file"]), verify=False)
                for item in summaries_batch
            ]
            observations, records = shard_batch(shards)
            observations = observations.to_model_input(args.device, float_dtype=torch.float32)
            targets = build_event_targets(observations, records, catalog, event_config=event_config)
            state_t = model.initial_state(observations.batch_size, device=args.device)
            episode_start = torch.ones(observations.batch_size, dtype=torch.bool, device=args.device)
            output = model(
                observations, state_t, episode_start=episode_start, card_row=targets.card_row,
                cell=targets.cell.clamp_min(0),
            )
            loss, parts = event_il_loss(output, targets, event_config=event_config, delay_neighbor_weight=0.0)
            loss_sums["loss"] += float(loss)
            for name in ("gate", "card", "delay", "cell"):
                loss_sums[name] += float(parts[name])

            gate_pred = output.gate_logits.argmax(dim=-1)
            counts["rows"] += int(targets.gate.numel())
            counts["gate_total"] += int(targets.gate.numel())
            counts["gate_correct"] += int((gate_pred == targets.gate).sum().item())

            act = targets.act_mask
            if bool(act.any()):
                card_logits = output.card_logits[act]
                card_target = targets.card_row[act]
                card_pred = card_logits.argmax(dim=-1)
                counts["act_total"] += int(card_target.numel())
                card_ok = card_pred == card_target
                counts["card_correct"] += int(card_ok.sum().item())
                top3 = card_logits.topk(min(3, card_logits.shape[-1]), dim=-1).indices
                counts["card_top3"] += int((top3 == card_target[:, None]).any(dim=-1).sum().item())

                delay_logits = output.delay_logits[act]
                delay_target = targets.delay_bin[act]
                delay_pred = delay_logits.argmax(dim=-1)
                counts["delay_correct"] += int((delay_pred == delay_target).sum().item())
                counts["delay_within1"] += int(((delay_pred - delay_target).abs() <= 1).sum().item())
                counts["delay_within3"] += int(((delay_pred - delay_target).abs() <= 3).sum().item())
                counts["delay_coarse_correct"] += int(
                    ((delay_pred // args.delay_coarse_group) == (delay_target // args.delay_coarse_group)).sum().item()
                )

                cell_ok = torch.zeros_like(card_ok)
                cell_ok_near = torch.zeros_like(card_ok)
                cell_sel = targets.cell_mask[act]
                if bool(cell_sel.any()):
                    cell_logits = output.cell_logits[act][cell_sel]
                    cell_target = targets.cell[act][cell_sel]
                    cell_pred = cell_logits.argmax(dim=-1)
                    counts["cell_total"] += int(cell_target.numel())
                    counts["cell_correct"] += int((cell_pred == cell_target).sum().item())
                    dx = (cell_pred % width) - (cell_target % width)
                    dy = (cell_pred // width) - (cell_target // width)
                    near = (dx.abs() <= 1) & (dy.abs() <= 1)
                    counts["cell_within1"] += int(near.sum().item())
                    cell_ok[cell_sel] = cell_pred == cell_target
                    cell_ok_near[cell_sel] = near
                counts["joint_correct"] += int(
                    (card_ok & ((delay_pred - delay_target).abs() <= 1) & cell_ok_near).sum().item()
                )

    def ratio(num: int, den: int) -> float:
        return round(num / den, 6) if den else 0.0

    report = {
        "checkpoint": str(args.checkpoint),
        "sampled_shards": len(selected),
        "rows": counts["rows"],
        "mean_loss": round(loss_sums["loss"] / max(1, len(batches)), 6),
        "mean_ce": {name: round(loss_sums[name] / max(1, len(batches)), 6) for name in ("gate", "card", "delay", "cell")},
        "accuracy": {
            "gate": ratio(counts["gate_correct"], counts["gate_total"]),
            "card_top1": ratio(counts["card_correct"], counts["act_total"]),
            "card_top3": ratio(counts["card_top3"], counts["act_total"]),
            "delay_exact": ratio(counts["delay_correct"], counts["act_total"]),
            "delay_within1": ratio(counts["delay_within1"], counts["act_total"]),
            "delay_within3": ratio(counts["delay_within3"], counts["act_total"]),
            "delay_coarse_acc": ratio(counts["delay_coarse_correct"], counts["act_total"]),
            "cell_exact": ratio(counts["cell_correct"], counts["cell_total"]),
            "cell_within1_tile": ratio(counts["cell_within1"], counts["cell_total"]),
            "joint_card_delay1_cell1": ratio(counts["joint_correct"], counts["act_total"]),
        },
        "counts": counts,
        "baselines": {
            "gate_uniform": 0.5,
            "card_top1_uniform": round(1 / 8, 6),
            "delay_exact_uniform": round(1 / event_config.delay_bin_count, 6),
            "delay_within1_uniform": round(3 / event_config.delay_bin_count, 6),
            "cell_exact_uniform": round(1 / (width * event_config.board_height), 6),
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
