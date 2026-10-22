"""Verified Hog PPO plus PUCT distillation; integration checks are isolated."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "runtime"), str(ROOT.parents[1])]

import torch
from distillation import scheduled_weights, split_episodes
from teacher_training import TeacherObjective, load_teachers
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.training.event_checkpoint import load_event_checkpoint, save_event_checkpoint
from native_runner.training.event_cache import load_event_rl_shard
from native_runner.training.event_ppo import EventPpoConfigV1, event_ppo_update
from native_runner.training.event_preflight import load_complete_rollout, preflight_event_rollout
from optimizer_state import (restore_optimizer, optimizer_checkpoint, optimizer_signature,
                             verify_optimizer_signature)


def _stream_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def formal_blockers(config, *, teachers, summaries, fleet_report, prototype):
    errors = []
    if not fleet_report or time.time() - fleet_report.get("checked_at", 0) > 300:
        errors.append("fresh five-host resource and task-engine inventory is required")
    if fleet_report and (fleet_report.get("passed") is not True or fleet_report.get("blockers")):
        errors.append("five-host native response and runtime hash preflight must pass")
    for host, required in config["fleet"].items():
        state = (fleet_report or {}).get("hosts", {}).get(host, {})
        if state.get("disk_free_gib", 0) < 20:
            errors.append(f".{host}: free disk {state.get('disk_free_gib', 0):.1f} GiB is below 20 GiB")
        if state.get("memory_available_gib", 0) < 32:
            errors.append(f".{host}: verified available RAM is below 32 GiB")
        if state.get("ready_task_engines", 0) < required:
            errors.append(f".{host}: {state.get('ready_task_engines', 0)}/{required} task engines ready")
    if (not prototype or prototype.get("passed") is not True or prototype.get("games") != 108 or
            prototype.get("simulations") != config["tree_search"]["prototype_simulations"]):
        errors.append("108 complete games with 32-simulation PUCT prototype acceptance are required")
    episodes = {record.episode_id for record in teachers}
    if len(episodes) != config["teacher_games_per_cycle"]:
        errors.append(f"teacher cycle has {len(episodes)}/{config['teacher_games_per_cycle']} complete episodes")
    if any(record.search_simulations not in (config["tree_search"]["teacher_simulations"],
                                             config["tree_search"]["high_uncertainty_simulations"])
           for record in teachers):
        errors.append("formal teacher records require 128 or 256 simulations")
    if len(summaries) != sum(config["round_games"].values()):
        errors.append(f"PPO update has {len(summaries)}/108 independent on-policy episode shards")
    return errors


def update_once(*, checkpoint, teacher_paths, rollout_root, output, mode="integration", update_index=1,
                device="cuda:2", batch_size=8, fleet_report=None, prototype=None,
                algorithm="legacy", config_path=None, plan_path=None,
                teacher_checkpoint_sha256=None, teacher_iteration=None, trust_attempts=4):
    if mode not in {"integration", "formal"}:
        raise ValueError("unknown training mode")
    if algorithm not in {"legacy", "firstlight-v2"}:
        raise ValueError("unknown versioned training algorithm")
    config = json.loads(Path(config_path or ROOT / "config.json").read_text())
    plan = json.loads(Path(plan_path or ROOT / "match-plan.json").read_text())
    if not 1 <= update_index <= config["optimizer"]["ppo_updates"]:
        raise ValueError("update index falls outside the approved 200-update budget")
    report_path = (Path(output).with_suffix(".report.json") if algorithm == "firstlight-v2" else
                   ROOT / ("formal-start-attempt.json" if mode == "formal" else "training-check/result.json"))
    summaries = sorted(Path(rollout_root).glob("**/part-*.json"))
    teacher_paths = [Path(path) for path in teacher_paths]
    data_error = None
    try:
        if algorithm == "firstlight-v2":
            from research.firstlight_v2.records import load_policy_teachers
            teachers = load_policy_teachers(teacher_paths) if teacher_paths else []
        else:
            teachers = load_teachers(teacher_paths) if teacher_paths else []
    except (OSError, ValueError, TypeError, KeyError) as error:
        if mode != "formal":
            raise
        teachers, data_error = [], f"teacher dataset verification failed: {error}"
    all_teacher_episodes = {record.episode_id for record in teachers}
    all_teachers = tuple(teachers)
    if mode == "formal":
        errors = formal_blockers(config, teachers=teachers, summaries=summaries,
                                 fleet_report=fleet_report, prototype=prototype)
        if data_error is not None:
            errors.append(data_error)
        if errors:
            result = dict(status="blocked_before_training", mode=mode, optimizer_steps=0,
                          checked_at=time.time(), blockers=errors)
            write(report_path, result)
            print(json.dumps(result, indent=2), flush=True)
            return result
        groups = split_episodes(teachers)
        if any(not group for group in groups.values()):
            raise ValueError("formal teacher train/validation/test episode splits must all be populated")
        teachers = groups["train"]
    if not teachers or not summaries:
        raise ValueError("a verified teacher dataset and separate complete on-policy episodes are required")
    output = Path(output)
    if output.exists():
        raise ValueError("training output already exists")
    if mode == "integration" and not output.resolve().is_relative_to((ROOT / "training-check").resolve()):
        raise ValueError("integration checkpoints must remain under training-check")
    torch.set_num_threads(1)
    torch.manual_seed(1755 + update_index)
    loaded = load_event_checkpoint(checkpoint, device=device)
    teacher_episodes = all_teacher_episodes
    quotas = {key: 0 for key in config["round_games"]}
    for summary in summaries:
        shard = load_event_rl_shard(summary, verify=True)
        contract = shard.manifest["contract"]
        if (contract.get("collection_mode") != "on-policy" or contract.get("search_enabled") is not False or
                contract.get("learner_deck_id") != "hog" or
                any(row.match_id in teacher_episodes for row in shard.records)):
            raise ValueError("PPO input must contain independent on-policy Hog episodes")
        if mode == "formal":
            kind = contract.get("opponent_kind")
            if kind not in quotas:
                raise ValueError("formal PPO shard is missing its frozen opponent allocation")
            expected_opponent = (loaded.checkpoint_sha256 if kind == "self_play" else
                                 config["opponent_checkpoints"][kind]["sha256"])
            expected_iteration = (None if kind == "self_play" else
                                  config["opponent_checkpoints"][kind]["iteration"])
            if (contract.get("opponent_sha256") != expected_opponent or
                    contract.get("opponent_iteration") != expected_iteration):
                raise ValueError("formal PPO shard opponent identity differs from the allocation")
            quotas[kind] += 1
    if mode == "formal" and quotas != config["round_games"]:
        raise ValueError("formal PPO round must contain 72 self, 12 fixed-1250 and 24 recent opponents")
    if algorithm == "firstlight-v2":
        import json as _json
        expected_teacher = teacher_checkpoint_sha256
        cycle_size = config["optimizer"]["updates_per_cycle"]
        required_teacher_iteration = config["start_iteration"] + ((update_index - 1) // cycle_size) * cycle_size
        if teacher_iteration != required_teacher_iteration:
            raise ValueError("teacher must be pinned to this 20-update cycle parent")
        reward_sha = hashlib.sha256(_json.dumps(plan["source_training_state"]["reward_config"],
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        content_sha = hashlib.sha256(_json.dumps(plan["learner_deck_identity"],
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        from runtime_contract import PROBE_SHA256
        if (expected_teacher is None or any(r.learner_sha256 != expected_teacher
                or r.opponent_sha256 != expected_teacher or r.reward_sha256 != reward_sha
                or r.content_pack_sha256 != content_sha or r.native_runtime_sha256 != PROBE_SHA256
                or str(r.terminal_evidence.get("reason", "")).startswith("SYNTHETIC") for r in all_teachers)):
            raise ValueError("v4 teachers differ from the pinned cycle model, runtime, content or reward contract")
        if update_index > 1 and loaded.payload.get("hog_tree_training", {}).get("algorithm") != algorithm:
            raise ValueError("algorithm version changed inside a continuation lineage")
    saved = loaded.payload["event_ppo_training"]
    if saved["iterations"] != config["start_iteration"] + update_index - 1:
        raise ValueError("parent checkpoint iteration differs from the requested update")
    if update_index == 1 and loaded.checkpoint_sha256 != config["start_checkpoint_sha256"]:
        raise ValueError("first update must resume the exact 1755 checkpoint")
    if mode == "formal" and update_index > 1:
        lineage = loaded.payload.get("hog_tree_training", {})
        if (lineage.get("mode") != "formal" or lineage.get("task_id") != ROOT.name or
                lineage.get("update_index") != update_index - 1):
            raise ValueError("formal continuation requires the preceding formal checkpoint")
    if (loaded.payload.get("rl_reference_policy") != {"version": "reference-disabled.v1", "enabled": False} or
            loaded.payload.get("kl_coefficient") != 0 or "event_component_gae" in loaded.payload):
        raise ValueError("parent checkpoint differs from the scalar-GAE/no-reference continuation")
    metadata = []
    lanes, counts = load_complete_rollout(
        summaries, expected_checkpoint_sha256=loaded.checkpoint_sha256,
        expected_model_contract_hash=loaded.contract_sha256, row_metadata=metadata,
        expected_minimum_delay_ms=100,
    )
    if counts["reward_config"] != plan["source_training_state"]["reward_config"]:
        raise ValueError("PPO reward contract differs from the frozen 1755 plan")
    lanes = [replace(lane, deck_id="hog") for lane in lanes]
    model = loaded.model
    prepared = PreparedEventRollout(model, lanes, row_batch_size=512, minibatch_lanes=0,
                                    device_cache_bytes=2048 * 1024**2, bucket_by_entities=True)
    preflight = preflight_event_rollout(model, lanes, prepared=prepared, row_metadata=metadata,
                                       expected_temperatures=(1., 1., 1., 1.))
    print(json.dumps(dict(phase="preflight", mode=mode, **preflight.as_dict())), flush=True)
    optimizer, progress = restore_optimizer(model, loaded.payload)
    before_signature = optimizer_signature(loaded.payload)
    validation_before = validation_after = None
    if algorithm == "firstlight-v2" and mode == "formal":
        from research.firstlight_v2.teacher import evaluate_teachers
        validation_before = evaluate_teachers(model, groups["validation"], microbatch=batch_size)
    before_parameters = {name: value.detach().cpu().clone() for name, value in model.named_parameters()}
    policy_weight, value_weight = scheduled_weights(update_index, **{
        "total_updates": config["distillation"]["total_updates"],
        "start": config["distillation"]["decay_start_update"],
        "policy_start": config["distillation"]["policy_start"],
        "value_start": config["distillation"]["value_start"],
        "policy_end": config["distillation"]["policy_end"],
        "value_end": config["distillation"]["value_end"],
    })
    if algorithm == "firstlight-v2":
        from research.firstlight_v2.teacher import CoverageTeacherObjective
        from research.firstlight_v2.transaction import guarded_update
        # Explicit algorithm change: the PPO critic never regresses to action-risk Q.
        value_weight = 0.
        def attempt():
            nonlocal objective
            objective = CoverageTeacherObjective(teachers, policy_weight=policy_weight, value_weight=0.,
                microbatch=batch_size, weighting="episode", seed=1755 + update_index, cache_bytes=0)
            stats = event_ppo_update(model, lanes, optimizer, prepared=prepared, auxiliary_losses=objective,
                config=EventPpoConfigV1(epochs=1, minibatch_lanes=0, gamma=.999, gae_lambda=.95,
                    kl_coefficient=0., target_kl=.01), expected_temperatures=(1., 1., 1., 1.))
            return stats, objective.report()
        objective = None
        stats, teacher_report, trust_report = guarded_update(model, optimizer, attempt, max_kl=.01,
                                                           max_attempts=trust_attempts)
    else:
        objective = TeacherObjective(teachers, policy_weight=policy_weight, value_weight=value_weight,
                                      batch_size=batch_size, seed=1755 + update_index)
        stats = event_ppo_update(
            model, lanes, optimizer, prepared=prepared, auxiliary_losses=objective,
            config=EventPpoConfigV1(epochs=1, minibatch_lanes=0, gamma=.999, gae_lambda=.95,
                                    kl_coefficient=0., target_kl=.01),
            expected_temperatures=(1., 1., 1., 1.),
        )
        trust_report = None
    if algorithm == "firstlight-v2" and mode == "formal":
        validation_after = evaluate_teachers(model, groups["validation"], microbatch=batch_size)
    if stats.optimizer_steps != 1 or objective.report()["records"] < 1:
        raise ValueError("one combined PPO/teacher optimizer update was required")
    json.dumps(stats.as_dict(), allow_nan=False)
    json.dumps(objective.report(), allow_nan=False)
    changed = sum(not torch.equal(before_parameters[name], parameter.detach().cpu())
                  for name, parameter in model.named_parameters())
    if changed == 0 or any(not torch.isfinite(parameter).all() for parameter in model.parameters()):
        raise ValueError("combined update has no parameter change or nonfinite parameters")
    reserved = {"event_initialization", "model", "event_checkpoint_contract", "contract_sha256",
                "checkpoint_sha256", "snapshot_only", "model_scale", "model_config", "event_config", "backbone_config"}
    extra = {key: value for key, value in loaded.payload.items() if key not in reserved}
    # The old mixed-deck controller does not govern this Hog-only experiment.
    extra.pop("mixed_deck_controller", None)
    extra.pop("rl_mixed_opponents", None)
    extra.update(parent_checkpoint_sha256=loaded.checkpoint_sha256,
                 event_ppo_training=optimizer_checkpoint(model, optimizer, progress, stats),
                 rl_steps=stats.steps, policy_version=f"hog-puct-{1755 + update_index}",
                 rl_action_constraints=counts["action_constraints"],
                 rl_reward_components=counts["reward_components"],
                 rl_execution_counts={key: counts[key] for key in (
                     "rows", "trainable_rows", "excluded_rows", "empty_training_lanes", "execution_status_counts")},
                 hog_tree_training=dict(mode=mode, task_id=ROOT.name, update_index=update_index,
                                        algorithm=algorithm, trust_gate=trust_report,
                                        teacher_checkpoint_sha256=teacher_checkpoint_sha256, teacher_iteration=teacher_iteration,
                                        validation_before=validation_before, validation_after=validation_after,
                                        teacher=objective.report(), teacher_files=[{
                                            "name": path.name, "sha256": _stream_sha256(path)
                                        } for path in teacher_paths]))
    advanced = verify_optimizer_signature(before_signature, optimizer_signature(extra), 1, preserve_groups=True)
    candidate_output = output.with_name("." + output.name + ".candidate") if algorithm == "firstlight-v2" else output
    checksum = save_event_checkpoint(candidate_output, model, extra=extra)
    child = load_event_checkpoint(candidate_output, device="cpu")
    if child.checkpoint_sha256 != checksum or child.payload["parent_checkpoint_sha256"] != loaded.checkpoint_sha256:
        raise ValueError("saved combined checkpoint failed readback")
    _, child_progress = restore_optimizer(child.model, child.payload)
    if child_progress != dict(iterations=progress["iterations"] + 1,
                              optimizer_steps=progress["optimizer_steps"] + 1,
                              decision_rows=progress["decision_rows"] + stats.steps):
        raise ValueError("saved training counters failed readback")
    verify_optimizer_signature(before_signature, optimizer_signature(child.payload), 1, preserve_groups=True)
    if algorithm == "firstlight-v2":
        candidate_output.replace(output)
    result = dict(status="updated", mode=mode, algorithm=algorithm, trust_gate=trust_report,
                  teacher_checkpoint_sha256=teacher_checkpoint_sha256, teacher_iteration=teacher_iteration,
                  validation_before=validation_before, validation_after=validation_after, formal_training_started=mode == "formal",
                  parent_checkpoint_sha256=loaded.checkpoint_sha256, checkpoint_sha256=checksum,
                  output=str(output), iteration=child.payload["event_ppo_training"]["iterations"],
                  optimizer_steps=stats.optimizer_steps, changed_parameters=changed, advanced_optimizer_states=advanced,
                  teacher=objective.report(), preflight=preflight.as_dict(), ppo=stats.as_dict(),
                  learning_rates=[group["lr"] for group in optimizer.param_groups])
    write(report_path, result)
    print(json.dumps(result, indent=2, allow_nan=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("formal", "integration"), default="formal")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--teacher", dest="teacher_paths", nargs="*", default=[])
    parser.add_argument("--rollout-root", type=Path, default=ROOT / "formal/ppo/update-001")
    parser.add_argument("--output", type=Path, default=ROOT / "formal/checkpoints/1756.pt")
    parser.add_argument("--update-index", type=int, default=1)
    parser.add_argument("--device", default="cuda:2")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--fleet-report", type=Path, default=ROOT / "fleet-preflight.json")
    parser.add_argument("--prototype", type=Path, default=ROOT / "prototype_acceptance.json")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    args.checkpoint = args.checkpoint or (ROOT / config["start_checkpoint"]).resolve()
    args.fleet_report = json.loads(args.fleet_report.read_text()) if args.fleet_report.exists() else None
    args.prototype = json.loads(args.prototype.read_text()) if args.prototype.exists() else None
    result = update_once(**vars(args))
    if result["status"] != "updated":
        raise SystemExit(2)
