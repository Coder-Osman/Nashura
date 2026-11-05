"""Offline tower-input sensitivity for identity-checked snapshot event critics.

No training, native connection, or action sampling. Only tower features 0 and 5
are scaled together; masks, activation, maxima, events and other fields stay
bitwise fixed. Positive retention avoids inventing destruction transitions.
King interventions are input diagnostics, not claims of reachable game states.
"""
from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass, replace
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
import torch

from .event_cache import load_event_rl_shard
from .event_checkpoint import load_event_checkpoint
from .event_tower_rewards import tower_config
from .v4.factory import production_semantic_bundle


RETENTIONS = (1., .75, .5, .25, .1)
GROUPS = {"own_princess": (0, "princess"), "enemy_princess": (1, "princess"),
          "own_king": (0, "king"), "enemy_king": (1, "king"),
          "own_all": (0, "all"), "enemy_all": (1, "all")}


def tensor_fields(value, prefix=""):
    if isinstance(value, torch.Tensor):
        yield prefix, value
    elif is_dataclass(value):
        for field in fields(value):
            yield from tensor_fields(getattr(value, field.name), prefix + "." + field.name)


def tensor_sha(items):
    digest = hashlib.sha256()
    for name, tensor in sorted(items):
        tensor = tensor.detach().cpu().contiguous()
        digest.update(json.dumps([name, str(tensor.dtype), list(tensor.shape)]).encode())
        digest.update(tensor.numpy().tobytes())
    return digest.hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tower_mask(frame, group):
    owner, kind = GROUPS[group]
    towers = frame.towers
    result = towers.mask & (towers.owner_type == owner)
    if kind != "all":
        result = result & ((towers.tower_type == 0) if kind == "king" else (towers.tower_type != 0))
    return result & (towers.features[..., 0] > 0) & (towers.features[..., 5] > 0)


def scale_tower_hp(frame, group, retention):
    if not 0 < retention <= 1:
        raise ValueError("retention must be finite and in (0, 1]")
    mask = tower_mask(frame, group)
    features = frame.towers.features.clone()
    for index in (0, 5):
        features[..., index] = torch.where(mask, features[..., index] * retention, features[..., index])
    return replace(frame, towers=replace(frame.towers, features=features))


def verify_intervention(original, changed):
    before, after = dict(tensor_fields(original)), dict(tensor_fields(changed))
    assert before.keys() == after.keys()
    for path, value in before.items():
        other = after[path]
        if path == ".towers.features":
            other = other.clone()
            other[..., 0], other[..., 5] = value[..., 0], value[..., 5]
        if not torch.equal(value, other):
            raise AssertionError("non-HP field changed: " + path)


def potential(frame):
    """The checkpoint's visible-tower-hp.v1 potential, in relative-owner order."""
    towers = frame.towers
    sign = torch.where(towers.owner_type == 0, 1., -1.)
    weight = torch.where(towers.tower_type == 0, 3., 1.)
    # Sum sign * weighted remaining fractions = weighted enemy minus own damage.
    return float((sign * weight * towers.features[..., 0] * towers.mask).sum() / 5.)


def validate_frame(frame, config):
    bundle = production_semantic_bundle()
    frame.validate(config, card_vocab_size=bundle.card_catalog.vocab_size,
                   ability_vocab_size=bundle.ability_catalog.vocab_size,
                   entity_archetype_vocab_size=bundle.entity_archetype_catalog.vocab_size,
                   effect_vocab_size=bundle.effect_catalog.vocab_size)
    towers = frame.towers
    if int(towers.mask.sum()) != 6:
        raise ValueError("diagnostic requires all six canonical tower rows")
    pairs = sorted(zip(towers.owner_type[towers.mask].tolist(), towers.tower_type[towers.mask].tolist()))
    if pairs != [(o, k) for o in (0, 1) for k in (0, 1, 2)]:
        raise ValueError("tower identities are incomplete or duplicated")
    ratio, absolute, maximum = (towers.features[..., i] for i in (0, 5, 6))
    if not all(bool(torch.isfinite(x).all()) for x in (ratio, absolute, maximum)):
        raise ValueError("nonfinite tower HP")
    if bool((maximum <= 0).any() or (ratio < 0).any() or (ratio > 1).any() or (absolute < 0).any()):
        raise ValueError("invalid HP or maximum")
    # Archives store the ratio and absolute features independently in FP16.
    if not torch.allclose(ratio * maximum, absolute, rtol=.002, atol=1e-5):
        raise ValueError("inconsistent ratio and absolute tower HP")


def predict(model, frame):
    result = model.encode(frame, model.initial_state(frame.batch_size)).value
    if not bool(torch.isfinite(result).all()):
        raise ValueError("nonfinite critic output")
    return result


def derivative_check(model, frame, group, *, center=.75, epsilon=.001):
    changed = scale_tower_hp(frame, group, center)
    features = changed.towers.features.detach().clone().requires_grad_(True)
    changed = replace(changed, towers=replace(changed.towers, features=features))
    gradient, = torch.autograd.grad(predict(model, changed).sum(), features)
    mask = tower_mask(frame, group)
    analytic = float(sum((gradient[..., i] * frame.towers.features[..., i] * mask).sum() for i in (0, 5)))
    with torch.no_grad():
        plus = float(predict(model, scale_tower_hp(frame, group, center + epsilon)))
        minus = float(predict(model, scale_tower_hp(frame, group, center - epsilon)))
    finite_difference = (plus - minus) / (2 * epsilon)
    return dict(analytic=analytic, finite_difference=finite_difference,
                absolute_error=abs(analytic - finite_difference), center=center, epsilon=epsilon)


def summarize(rows, *, coefficient=0., tolerance=1e-5):
    result = {}
    for name, (owner, _) in GROUPS.items():
        eligible = [row for row in rows if row["sweeps"][name]["changed_towers"] > 0]
        if not eligible:
            result[name] = {"states": 0}
            continue
        values = np.array([row["sweeps"][name]["values"] for row in eligible])
        phi = np.array([row["sweeps"][name]["potential"] for row in eligible])
        values = values + coefficient * phi
        # Retentions decrease left-to-right: own damage should lower value.
        desired_sign = -1 if owner == 0 else 1
        half_delta = values[:, 2] - values[:, 0]
        increments = desired_sign * np.diff(values, axis=1)
        result[name] = dict(states=len(eligible), mean_values=values.mean(0).tolist(),
            half_hp_delta_mean=float(half_delta.mean()), half_hp_delta_median=float(np.median(half_delta)),
            half_hp_delta_p05_p95=np.quantile(half_delta, [.05, .95]).tolist(),
            half_hp_direction_fraction=float((desired_sign * half_delta > tolerance).mean()),
            half_hp_sensitive_fraction=float((np.abs(half_delta) > tolerance).mean()),
            monotone_state_fraction=float((increments >= -tolerance).all(1).mean()),
            monotone_step_fraction=float((increments >= -tolerance).mean()),
            strict_direction_step_fraction=float((increments > tolerance).mean()),
            range_median=float(np.median(np.ptp(values, axis=1))))
    return result


def run(checkpoint, shard_paths, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    torch.set_num_threads(1)
    torch.manual_seed(20261105)
    loaded = load_event_checkpoint(checkpoint, device="cpu")
    model = loaded.model.eval().requires_grad_(False)
    if not model.backbone.config.snapshot_only:
        raise ValueError("recurrent checkpoints require an explicit history replay protocol")
    shaping = tower_config(loaded.payload.get("rl_reward_config"))
    coefficient = 0. if shaping is None else shaping["coefficient"]
    model_before = tensor_sha(list(model.named_parameters()) + list(model.named_buffers()))
    sources, seen_lanes, rows, derivative_rows, examples = [], set(), [], [], []
    repeat_error = 0.
    for path in map(Path, shard_paths):
        shard = load_event_rl_shard(path, verify=True)
        contract = shard.manifest["contract"]
        if contract.get("behavior_checkpoint_sha256") != loaded.checkpoint_sha256:
            raise ValueError("archive was not collected by this checkpoint")
        if contract.get("behavior_model_contract_hash") != loaded.contract_sha256:
            raise ValueError("archive/model contract mismatch")
        lanes = {}
        for index, record in enumerate(shard.records):
            lanes.setdefault((record.match_id, record.owner), []).append(index)
        for key, indices in lanes.items():
            lane = [shard.records[i] for i in indices]
            if key in seen_lanes or any(r.truncated for r in lane) or sum(r.done for r in lane) != 1 or not lane[-1].done:
                raise ValueError("duplicate, incomplete or truncated episode lane")
            if any(a.observation_tick >= b.observation_tick for a, b in zip(lane, lane[1:])):
                raise ValueError("non-increasing observation ticks")
            seen_lanes.add(key)
        source = dict(summary_sha256=file_sha(path), data_sha256=file_sha(shard.path),
                      frames=len(shard.records), lanes=len(lanes))
        sources.append(source)
        for index, record in enumerate(shard.records):
            frame = shard.observation(index).to_model_input("cpu")
            validate_frame(frame, model.backbone.config)
            before = tensor_sha(tensor_fields(frame))
            with torch.no_grad():
                baseline = float(predict(model, frame))
                row = dict(source_sha256=source["data_sha256"], index=index,
                           match_id=record.match_id, owner=record.owner, tick=record.observation_tick,
                           frame_sha256=before, baseline=baseline, sweeps={})
                for group in GROUPS:
                    values, potentials = [], []
                    for retention in RETENTIONS:
                        changed = scale_tower_hp(frame, group, retention)
                        verify_intervention(frame, changed)
                        values.append(baseline if retention == 1 else float(predict(model, changed)))
                        potentials.append(potential(changed))
                    row["sweeps"][group] = dict(changed_towers=int(tower_mask(frame, group).sum()),
                                               values=values, potential=potentials)
                repeat_error = max(repeat_error, abs(float(predict(model, frame)) - baseline))
            if index % 16 == 0:
                for group in ("own_princess", "enemy_princess"):
                    derivative_rows.append(dict(index=index, source_sha256=source["data_sha256"],
                                                group=group, **derivative_check(model, frame, group)))
            if before != tensor_sha(tensor_fields(frame)):
                raise AssertionError("source frame mutated")
            rows.append(row)
            with (output / "states.jsonl").open("a") as stream:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
            if index in (0, 40, 80, 120):
                examples.append({k: row[k] for k in ("index", "tick", "baseline", "sweeps")})
            if (index + 1) % 16 == 0:
                print(json.dumps(dict(states=len(rows), seconds=round(time.perf_counter() - started, 2))), flush=True)
        if source["summary_sha256"] != file_sha(path) or source["data_sha256"] != file_sha(shard.path):
            raise AssertionError("archive changed while evaluating")
    if not rows:
        raise ValueError("no archived states")
    model_after = tensor_sha(list(model.named_parameters()) + list(model.named_buffers()))
    if model_after != model_before or loaded.checkpoint_sha256 != file_sha(checkpoint):
        raise AssertionError("model or checkpoint mutated")
    source_root = Path(__file__).resolve().parent
    source_code = {name: file_sha(source_root / name) for name in
                   ("event_value_sensitivity.py", "event_checkpoint.py", "event_model.py", "event_cache.py",
                    "event_tower_rewards.py", "v4/model.py", "v4/components.py", "v4/config.py", "v4/tensors.py",
                    "v4/tensorizer.py", "v4/factory.py", "v4/mechanics.py")}
    report = dict(schema="event-tower-input-sensitivity.v1", checkpoint_sha256=loaded.checkpoint_sha256,
        source_code=source_code,
        contract_sha256=loaded.contract_sha256, iteration=loaded.payload.get("event_ppo_training", {}).get("iterations"),
        model_parameters=model.parameter_count(), snapshot_only=True, residual_blocks=model.config.residual_blocks,
        value_semantics="unbounded scalar shaped-return estimate; not WDL probability",
        runtime=dict(torch=torch.__version__, python=platform.python_version(), machine=platform.machine(),
                     device="cpu", threads=1), retentions=RETENTIONS, tolerance=1e-5,
        sources=sources, states=len(rows), episode_lanes=len(seen_lanes),
        matches=len({key[0] for key in seen_lanes}), owners=sorted({row["owner"] for row in rows}),
        raw_value=summarize(rows), potential_coefficient=coefficient,
        potential_adjusted_value=summarize(rows, coefficient=coefficient),
        potential_adjustment="V + beta*Phi removes the known shaping offset for an exact discounted critic; does not calibrate this approximate critic",
        input_gradient_checks=derivative_rows, examples=examples,
        integrity=dict(parameters_and_buffers_unchanged=model_before == model_after,
                       model_tensors_sha256=model_before, checkpoint_unchanged=True, source_files_unchanged=True,
                       non_hp_inputs_unchanged=True, source_frames_unchanged=True, repeat_output_max_abs=repeat_error,
                       parameter_gradients_absent=all(p.grad is None for p in model.parameters())),
        limitation="One or more archived lanes; repeated states are correlated, no independent-match CI or win-rate claim. Positive HP interventions hold activation/history fixed, king cases can be off-manifold.",
        seconds=time.perf_counter() - started)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(dict(status="complete", states=len(rows), seconds=report["seconds"])), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--shard", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.checkpoint, args.shard, args.output)


if __name__ == "__main__":
    main()
