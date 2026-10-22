"""Persistent PPO worker and independent checkpoint readback worker.

Exactly one request is in flight. Any failed request terminates the worker;
recovery always starts from an archived, completed-round checkpoint.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.event_inference import export_inference
from native_runner.training.event_component_gae import (
    COMPONENT_KEY, load_critic, resolve_config, restore_component_optimizer,
)
from tools.event_rl_update import PPO_TRAINING_KEY, UpdateSession, parse_args, restore_optimizer


def training_metadata(loaded):
    if not loaded.model.backbone.config.snapshot_only:
        raise ValueError("persistent learner requires snapshot_only")
    if any(not torch.isfinite(t).all() for t in loaded.model.state_dict().values()):
        raise ValueError("nonfinite model state")
    optimizer, progress = restore_optimizer(loaded.model, loaded.payload)
    for parameter, state in optimizer.state.items():
        for key, tensor in state.items():
            if isinstance(tensor, torch.Tensor):
                if not torch.isfinite(tensor).all():
                    raise ValueError("nonfinite optimizer state")
                if key in {"exp_avg", "exp_avg_sq", "max_exp_avg_sq"} and tensor.shape != parameter.shape:
                    raise ValueError("optimizer moment shape mismatch")
    result = dict(checkpoint_sha256=loaded.checkpoint_sha256,
                  contract_sha256=loaded.contract_sha256, **progress)
    config = resolve_config(loaded.payload, gamma=loaded.payload.get("rl_gae_config", {}).get("gamma", .999))
    if config is not None:
        critic = load_critic(loaded.model, loaded.payload, config)
        restore_component_optimizer(critic, loaded.payload)
        saved = loaded.payload[COMPONENT_KEY]
        result["component_gae"] = dict(config=config.as_dict(),
            **{k: saved[k] for k in ("updates", "optimizer_steps", "decision_rows")})
    return result


def optimizer_signature(payload):
    saved = payload.get(PPO_TRAINING_KEY)
    if saved is None:
        return None
    return dict(names=saved['parameter_names'], groups=saved['optimizer']['param_groups'],
                state={key: dict(step=float(value.get('step', 0)),
                                 shapes={k: tuple(v.shape) for k,v in value.items()
                                         if k in {'exp_avg', 'exp_avg_sq', 'max_exp_avg_sq'}})
                       for key,value in saved['optimizer']['state'].items()})


def verify_optimizer_signature(parent, child, steps, *, preserve_groups=False):
    if child is None:
        raise ValueError('child is missing optimizer state')
    if parent is None:
        return len(child['state'])
    # Learning rate is an explicit supported CLI override; all other AdamW options must survive.
    without_lr = lambda groups: [{k:v for k,v in g.items() if k != 'lr'} for g in groups]
    if (parent['names'] != child['names'] or without_lr(parent['groups']) != without_lr(child['groups'])
            or (preserve_groups and parent['groups'] != child['groups'])):
        raise ValueError('optimizer parameter groups changed')
    advanced = 0
    for key, old in parent['state'].items():
        new = child['state'].get(key)
        if new is None or new['shapes'] != old['shapes']:
            raise ValueError('optimizer moments disappeared or changed shape')
        delta = new['step']-old['step']
        if delta < 0 or delta > steps or delta != int(delta):
            raise ValueError('optimizer per-parameter step continuity mismatch')
        advanced += int(delta > 0)
    for key,new in child['state'].items():
        if key not in parent['state'] and not 0 <= new['step'] <= steps:
            raise ValueError('new optimizer state has an invalid step')
    return advanced


def checkpoint_details(payload):
    return dict(component_gae_present='event_component_gae' in payload, **{key: payload[key] for key in (
        'rl_reward_config', 'rl_reward_components', 'mixed_deck_controller', 'rl_reference_policy') if key in payload})


class CheckpointVerifier:
    """Cache only the verified parent identity/counters, never parent tensors."""

    def __init__(self, checkpoint, *, initialization_only=False):
        loaded = load_event_checkpoint(checkpoint, initialization_only=initialization_only)
        self.current = training_metadata(loaded)
        self.parent_optimizer = optimizer_signature(loaded.payload)
        self.last_details = checkpoint_details(loaded.payload)
        self.failed = False

    def verify(self, task):
        if self.failed:
            raise RuntimeError("verifier failed; restart from a verified checkpoint")
        try:
            parent = self.current
            if task["parent_checkpoint_sha256"] != parent["checkpoint_sha256"]:
                raise ValueError("verifier parent identity mismatch")
            loaded = load_event_checkpoint(task["output"])
            current = training_metadata(loaded)
            expected = {
                "checkpoint_sha256": task["checkpoint_sha256"],
                "contract_sha256": parent["contract_sha256"],
                "iterations": parent["iterations"] + 1,
                "optimizer_steps": parent["optimizer_steps"] + task["optimizer_steps"],
                "decision_rows": parent["decision_rows"] + task["decision_rows"],
            }
            if "component_gae" in parent or "component_gae" in current:
                previous = parent.get("component_gae", {})
                if "component_gae" not in current:
                    raise ValueError("component critic state disappeared")
                expected["component_gae"] = dict(
                    config=previous.get("config", current["component_gae"]["config"]),
                    updates=previous.get("updates", 0)+1,
                    optimizer_steps=previous.get("optimizer_steps", 0)+task["optimizer_steps"],
                    decision_rows=previous.get("decision_rows", 0)+task["decision_rows"])
            if task["iteration"] != expected["iterations"] or current != expected:
                raise ValueError("checkpoint identity or training counters mismatch")
            if loaded.payload.get("parent_checkpoint_sha256") != parent["checkpoint_sha256"]:
                raise ValueError("saved checkpoint parent mismatch")
            if task.get("cache_root"):
                plan_path = Path(task["cache_root"]).parent / "match-plan.json"
                if plan_path.exists():
                    from native_runner.training.event_opponents import validate_plan
                    plan = validate_plan(json.loads(plan_path.read_text()))
                    mixed = loaded.payload.get("rl_mixed_opponents", {})
                    if (mixed.get("plan_sha256") != plan["sha256"] or mixed.get("version") != plan["version"]
                            or mixed.get("trainable_rows") != task["decision_rows"]
                            or mixed.get("actual_training_lanes", 181) > plan["expected_player_lanes"]):
                        raise ValueError("checkpoint mixed opponent metadata mismatch")
            signature = optimizer_signature(loaded.payload)
            advanced = verify_optimizer_signature(self.parent_optimizer, signature, task['optimizer_steps'],
                preserve_groups=task.get('preserve_optimizer_groups', False))
            details = checkpoint_details(loaded.payload)
            if (task.get('reference_disabled') or self.last_details.get('rl_reference_policy', {}).get('enabled') is False):
                if (details.get('rl_reference_policy') != dict(version='reference-disabled.v1', enabled=False)
                        or loaded.payload.get('kl_coefficient') != 0):
                    raise ValueError('reference policy unexpectedly restored')
            self.current = current
            self.parent_optimizer = signature
            self.last_details = dict(details, advanced_parameters=advanced, optimizer_state_count=len(signature['state']))
            return current
        except BaseException:
            self.failed = True
            raise


class Learner:
    def __init__(self, config):
        self.args = parse_args(config["argv"])
        if self.args.preflight_only or self.args.output is None:
            raise ValueError("persistent learner requires checkpoint output")
        self.session = UpdateSession(self.args)
        from native_runner.training.event_shard_cache import VerifiedShardCache
        self.session.shard_cache = VerifiedShardCache(config.get('decoded_cache_mib', 2048)*1024**2)
        self.audit_pool = None
        self.audit_result = None
        self.audit_workers = config.get('audit_workers', 4)
        self.seed_base = config.get('seed_base')
        self.directory = Path(config["inference_directory"])
        self.descriptor = export_inference(self.args.checkpoint, self.directory,
                                          initialization_only=self.args.initialization_only)
        if self.descriptor["source_checkpoint_sha256"] != self.session.loaded.checkpoint_sha256:
            raise ValueError("initial inference export identity mismatch")

    def stage(self, task):
        from tools.event_rl_round_audit import RoundAuditPool
        directory = Path(task['directory']).resolve()
        if self.audit_pool is None:
            self.audit_pool = RoundAuditPool(directory, shard_cache=self.session.shard_cache,
                                            workers=self.audit_workers)
        if (self.audit_pool.directory != directory
                or self.audit_pool.plan['current_sha256'] != self.session.loaded.checkpoint_sha256):
            raise ValueError('staged audit parent or round identity mismatch')
        return self.audit_pool.submit(task['folder'])

    def audit(self, task):
        from tools.event_rl_round_audit import RoundAuditPool
        directory = Path(task['directory']).resolve()
        if self.audit_pool is None:
            self.audit_pool = RoundAuditPool(directory, shard_cache=self.session.shard_cache,
                                            workers=self.audit_workers)
        if (self.audit_pool.directory != directory
                or self.audit_pool.plan['current_sha256'] != self.session.loaded.checkpoint_sha256):
            raise ValueError('audit parent or round identity mismatch')
        if self.audit_result is None:
            self.audit_result = self.audit_pool.finish()
        return dict(audit=self.audit_result)

    def close(self):
        if self.audit_pool is not None:
            self.audit_pool.close()
        self.session.shard_cache.clear()

    def update(self, task):
        loaded = self.session.loaded
        previous = loaded.payload.get(PPO_TRAINING_KEY, {})
        if task["parent_checkpoint_sha256"] != loaded.checkpoint_sha256:
            raise ValueError("learner parent identity mismatch")
        if task["iteration"] != previous.get("iterations", 0) + 1:
            raise ValueError("duplicate or out-of-order learner request")
        args = argparse.Namespace(**vars(self.args))
        for key in ("checkpoint", "cache_root", "output"):
            setattr(args, key, Path(task[key]))
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("learner output already exists")
        if self.audit_pool is not None:
            directory = args.cache_root.parent.resolve()
            self.audit(dict(directory=str(directory)))
            self.audit_pool.verify_evidence()
        if self.seed_base is not None:
            torch.manual_seed(self.seed_base + task['iteration'])
        started = time.perf_counter()
        with Path(task["log"]).open("x") as log, redirect_stdout(log), redirect_stderr(log):
            self.session.update(args)
            saved = time.perf_counter()
            self.descriptor = export_inference(args.output, self.directory)
            if self.descriptor["source_checkpoint_sha256"] != self.session.loaded.checkpoint_sha256:
                raise ValueError("child inference export identity mismatch")
        progress = self.session.progress
        cache_stats = self.session.shard_cache.as_dict()
        self.session.shard_cache.clear()
        self.audit_pool = self.audit_result = None
        return dict(timings=self.session.last_timings, decoded_cache=cache_stats, checkpoint_sha256=self.session.loaded.checkpoint_sha256,
                    descriptor=self.descriptor, iteration=progress["iterations"],
                    optimizer_steps=progress["optimizer_steps"]-previous.get("optimizer_steps", 0),
                    decision_rows=progress["decision_rows"]-previous.get("decision_rows", 0),
                    update_seconds=saved-started, export_seconds=time.perf_counter()-saved)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("learner", "verifier"))
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    started = time.perf_counter()
    if args.mode == "learner":
        worker = Learner(config)
        ready = dict(checkpoint_sha256=worker.session.loaded.checkpoint_sha256,
                     contract_sha256=worker.session.loaded.contract_sha256,
                     descriptor=worker.descriptor)
    else:
        worker = CheckpointVerifier(config["checkpoint"], initialization_only=config.get("initialization_only", False))
        ready = worker.current
    print(json.dumps(dict(ready=True, pid=os.getpid(), startup_seconds=time.perf_counter()-started,
                          **ready)), flush=True)
    for line in sys.stdin:
        task = json.loads(line)
        started = time.perf_counter()
        try:
            if args.mode == "learner":
                operation = task.get('operation', 'update')
                if operation not in {'stage', 'audit', 'update'}:
                    raise ValueError('unknown learner operation')
                result = getattr(worker, operation)(task)
            else:
                result = worker.verify(task)
            print(json.dumps(dict(success=True, request_id=task["request_id"], pid=os.getpid(),
                                  seconds=time.perf_counter()-started, **result)), flush=True)
        except BaseException:
            print(json.dumps(dict(success=False, request_id=task.get("request_id"),
                                  error=traceback.format_exc())), flush=True)
            if args.mode == "learner":
                worker.close()
            return 1
    if args.mode == "learner":
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
