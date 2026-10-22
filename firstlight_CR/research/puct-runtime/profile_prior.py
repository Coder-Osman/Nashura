"""Profile one frozen teacher root prior without starting a native episode."""
from __future__ import annotations

import argparse
import cProfile
import importlib.util
import io
import json
import math
from pathlib import Path
import pstats
from types import SimpleNamespace
import time

import torch

from event_policy_rollout import EventPolicyRollout
from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.v4.model import _distribution
from policy_rollout import _decode


ROOT = Path(__file__).resolve().parent


def batched_prior(rollout: EventPolicyRollout, owner: int) -> dict[str, float]:
    keys = rollout.legal_action_keys(owner)
    if any(key.startswith("ability:") for key in keys):
        raise ValueError("diagnostic only supports roots without legal abilities")
    model = rollout.models[owner]
    legal = rollout.legals[owner]
    with torch.inference_mode():
        context = rollout.context(owner)
        masks = rollout.masks(owner)
        gate_scores, gate_mask = model.event_gate(context, masks["gate_mask"])
        gate_log = _distribution(
            gate_scores, mask=gate_mask, temperature=rollout.temperatures[0], validate=False
        ).logits[0]
        card_log = _distribution(
            model.card_logits(context), mask=masks["card_mask"],
            temperature=rollout.temperatures[1], validate=False,
        ).logits[0]
        log_probs = {"wait": float(gate_log[0])}
        for row in legal.legal_rows:
            row_tensor = torch.tensor([row], dtype=torch.long, device=rollout.device)
            spatial_keys, cell_query, condition_input = model._cell_conditioning(context, row_tensor)
            cell_log = _distribution(
                model._score_cells(cell_query, spatial_keys),
                mask=legal.cell_masks[row].unsqueeze(0).to(rollout.device),
                temperature=rollout.temperatures[2], validate=False,
            ).logits[0]
            cells = legal.cell_masks[row].nonzero(as_tuple=False).flatten()
            selected = spatial_keys.flatten(2)[0].transpose(0, 1).index_select(0, cells)
            delay_logits = model.delay_head(torch.cat(
                (condition_input.expand(len(cells), -1), selected), dim=1
            ))
            delay_log = _distribution(
                delay_logits, mask=legal.delay_masks[row].unsqueeze(0).expand(len(cells), -1),
                temperature=rollout.temperatures[3], validate=False,
            ).logits
            joint = (gate_log[1] + card_log[row] + cell_log[cells, None] + delay_log).cpu()
            delays = legal.delay_masks[row].nonzero(as_tuple=False).flatten().tolist()
            for index, cell in enumerate(cells.tolist()):
                for delay in delays:
                    log_probs[f"play:{row}:{cell}:{delay}"] = float(joint[index, delay])
    if set(log_probs) != set(keys):
        raise ValueError("batched diagnostic prior differs from legal actions")
    maximum = max(log_probs.values())
    return {key: math.exp(value - maximum) for key, value in log_probs.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--device", default="cuda:7")
    parser.add_argument("--compare-batched", action="store_true")
    parser.add_argument("--candidate-module", type=Path)
    args = parser.parse_args()

    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False

    result = json.loads(args.record.read_text())
    teacher = result["teacher_record"]
    owner = result["root_actor"]
    frame = _decode(teacher["semantic_frame"])
    masks = _decode(teacher["action_masks"])
    config = json.loads((ROOT / "config.json").read_text())
    checkpoint = load_event_checkpoint((ROOT / config["start_checkpoint"]).resolve(), device=args.device)
    model = checkpoint.model.eval()
    device = next(model.parameters()).device
    legal = SimpleNamespace(
        act_allowed=bool(masks["gate_mask"][0, 1]),
        card_mask=masks["card_mask"][0].to(device),
        cell_masks=masks["cell_masks"][0].to(device),
        delay_masks=masks["delay_mask"][0].to(device),
        legal_rows=tuple(int(index) for index in masks["card_mask"][0].nonzero(as_tuple=False)),
    )
    rollout = EventPolicyRollout.__new__(EventPolicyRollout)
    rollout.ready = [owner]
    rollout.models = {owner: model}
    rollout.frames = {owner: frame}
    rollout.legals = {owner: legal}
    rollout.device = device
    rollout.config = model.event_config
    rollout.temperatures = (1.0, 1.0, 1.0, 1.0)

    with torch.inference_mode():
        rollout.context(owner)
    torch.cuda.synchronize(device)

    profiler = cProfile.Profile()
    profiler.enable()
    profile_started = time.perf_counter()
    prior = rollout.policy_prior(owner)
    torch.cuda.synchronize(device)
    profiled_seconds = time.perf_counter() - profile_started
    profiler.disable()

    baseline_started = time.perf_counter()
    baseline_prior = rollout.policy_prior(owner)
    torch.cuda.synchronize(device)
    baseline_seconds = time.perf_counter() - baseline_started
    if prior != baseline_prior or set(prior) != set(teacher["legal_actions"]):
        raise ValueError("profiled prior differs from the frozen legal-action contract")

    comparison = None
    if args.compare_batched:
        batched_prior(rollout, owner)
        torch.cuda.synchronize(device)
        batched_started = time.perf_counter()
        candidate = batched_prior(rollout, owner)
        torch.cuda.synchronize(device)
        batched_seconds = time.perf_counter() - batched_started
        if set(candidate) != set(prior):
            raise ValueError("batched prior action set differs")
        max_absolute_error = max(abs(prior[key] - candidate[key]) for key in prior)
        comparison = {
            "batched_seconds": batched_seconds,
            "speedup": baseline_seconds / batched_seconds,
            "max_absolute_error": max_absolute_error,
        }

    candidate_comparison = None
    if args.candidate_module is not None:
        spec = importlib.util.spec_from_file_location("candidate_event_policy_rollout", args.candidate_module)
        if spec is None or spec.loader is None:
            raise ValueError("candidate module could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate_prior = module.EventPolicyRollout.policy_prior
        candidate_prior(rollout, owner)
        torch.cuda.synchronize(device)
        candidate_started = time.perf_counter()
        candidate = candidate_prior(rollout, owner)
        torch.cuda.synchronize(device)
        candidate_seconds = time.perf_counter() - candidate_started
        if set(candidate) != set(prior):
            raise ValueError("candidate prior action set differs")
        max_absolute_error = max(abs(prior[key] - candidate[key]) for key in prior)
        candidate_comparison = {
            "candidate_seconds": candidate_seconds,
            "speedup": baseline_seconds / candidate_seconds,
            "max_absolute_error": max_absolute_error,
        }

    report = io.StringIO()
    pstats.Stats(profiler, stream=report).sort_stats("cumtime").print_stats(35)
    print(json.dumps({
        "episode_id": result["episode_id"],
        "device": str(device),
        "legal_actions": len(prior),
        "legal_rows": len(legal.legal_rows),
        "legal_cells": sum(int(legal.cell_masks[row].sum()) for row in legal.legal_rows),
        "baseline_seconds": baseline_seconds,
        "profiled_seconds": profiled_seconds,
        "comparison": comparison,
        "candidate_comparison": candidate_comparison,
    }, indent=2))
    print(report.getvalue())


if __name__ == "__main__":
    main()
