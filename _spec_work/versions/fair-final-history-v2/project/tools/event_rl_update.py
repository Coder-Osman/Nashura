"""Run event-space PPO iterations over collected rollout shards.

The second half of the P0 loop: ``tools/event_rl_rollout.py`` writes behaviour
shards, this consumes them. Lanes are rebuilt per owner and per match, scored
with the masks the behaviour policy sampled under, and updated with the
temperatures those rows were collected at — an importance ratio only means
something while collector and update agree.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

from native_runner.training.event_batches import PreparedEventRollout  # noqa: E402
from native_runner.training.event_component_gae import (  # noqa: E402
    COMPONENT_KEY, component_checkpoint, load_critic, resolve_config, restore_component_optimizer,
)
from native_runner.training.event_delay_constraints import MINIMUM_POLICY_DELAY_MS  # noqa: E402
from native_runner.training.event_preflight import (  # noqa: E402
    EventPreflightError,
    load_complete_rollout,
    preflight_event_rollout,
)
from native_runner.training.event_checkpoint import load_event_checkpoint, save_event_checkpoint  # noqa: E402
from native_runner.training.event_ppo import (  # noqa: E402
    EventPpoConfigV1,
    event_ppo_update,
    value_head_parameters,
    warm_up_value,
)


PPO_TRAINING_KEY = "event_ppo_training"


def restore_optimizer(model, checkpoint, *, learning_rate=None):
    """Resume PPO moments only; an IL optimizer is not an RL optimizer."""
    if learning_rate is not None and (not math.isfinite(learning_rate) or learning_rate < 0):
        raise ValueError("learning rate must be finite and nonnegative")
    progress = dict(iterations=0, optimizer_steps=0, decision_rows=0)
    saved = checkpoint.get(PPO_TRAINING_KEY)
    names = [name for name, _ in model.named_parameters()]
    if PPO_TRAINING_KEY in checkpoint:
        if not isinstance(saved, dict) or saved.get("version") != "event-ppo-training.v1":
            raise ValueError("invalid event PPO training state")
        if saved.get("optimizer_type") != "AdamW" or saved.get("parameter_names") != names:
            raise ValueError("event PPO optimizer type or parameter order mismatch")
        for key in progress:
            value = saved.get(key)
            if type(value) is not int or value < 0:
                raise ValueError(f"invalid event PPO {key}")
            progress[key] = value
        if not isinstance(saved.get("optimizer"), dict):
            raise ValueError("missing event PPO optimizer state")
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5 if learning_rate is None else learning_rate)
    if saved is not None:
        optimizer.load_state_dict(saved["optimizer"])
        # load_state_dict restores device-local moments and all AdamW options.
        # Only an explicit CLI learning rate overrides the saved rate.
        if learning_rate is not None:
            for group in optimizer.param_groups:
                group["lr"] = learning_rate
    return optimizer, progress


def optimizer_checkpoint(model, optimizer, progress, stats):
    return {
        "version": "event-ppo-training.v1",
        "optimizer_type": "AdamW",
        "parameter_names": [name for name, _ in model.named_parameters()],
        "optimizer": optimizer.state_dict(),
        "iterations": progress["iterations"] + 1,
        "optimizer_steps": progress["optimizer_steps"] + stats.optimizer_steps,
        "decision_rows": progress["decision_rows"] + stats.steps,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--initialization-only", action="store_true",
                        help="explicitly finetune migrated policy heads; keep evaluation pending")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--max-shards", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=None,
                        help="override learning rate; otherwise resume saved rate (new PPO run: 1e-5)")
    parser.add_argument("--target-kl", type=float, default=None, help="stop remaining steps/epochs after full-rollout post-step sampled KL reaches target; not a hard bound")
    parser.add_argument("--clip-ratio", type=float, default=0.2)
    parser.add_argument("--entropy-coefficient", type=float, default=0.01)
    parser.add_argument("--gamma", type=float, default=0.999)
    parser.add_argument("--gae-lambda", type=float, default=0.95)
    parser.add_argument("--component-gae", action="store_true",
                        help="split outcome/tower/overflow GAE (defaults .99/.90/.50); saved settings resume automatically")
    parser.add_argument("--outcome-gae-lambda", type=float, default=None)
    parser.add_argument("--tower-gae-lambda", type=float, default=None)
    parser.add_argument("--overflow-gae-lambda", type=float, default=None)
    parser.add_argument("--sequence-chunk-steps", type=int, default=32)
    parser.add_argument("--row-batch-size", type=int, default=512,
                        help="independent rows per packed batch; 0 uses legacy per-lane chunks")
    parser.add_argument("--no-entity-buckets", action="store_true",
                        help="use original row order for controlled benchmarks")
    parser.add_argument("--input-cache-mib", type=int, default=2048,
                        help="device budget for packed observations; overflow reuses CPU packing")
    parser.add_argument(
        "--minibatch-lanes",
        type=int,
        default=8,
        help="lanes per optimizer step; 0 means one batch of every lane",
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--mixed-deck-config", type=Path, default=None)
    parser.add_argument(
        "--reference-checkpoint",
        type=Path,
        default=None,
        help="legacy argument accepted for compatibility; no reference model is loaded",
    )
    parser.add_argument("--kl-coefficient", type=float, default=0.0, help="legacy argument; reference KL is disabled")
    parser.add_argument(
        "--kl-target",
        type=float,
        default=0.02,
        help="legacy argument; reference KL is disabled",
    )
    parser.add_argument("--kl-min", type=float, default=1e-4)
    parser.add_argument("--kl-max", type=float, default=5.0)
    parser.add_argument(
        "--bc-coefficient",
        type=float,
        default=0.0,
        help="weight of the filtered imitation of kept episodes (rejection sampling); 0 disables it",
    )
    parser.add_argument(
        "--bc-margin-threshold",
        type=float,
        default=0.0,
        help="an episode is kept when its terminal reward is above this; the weight is the margin above it",
    )
    parser.add_argument(
        "--value-warmup-steps",
        type=int,
        default=0,
        help="fit the critic to the episodes' results for this many steps before PPO (0 skips it)",
    )
    parser.add_argument("--value-warmup-lr", type=float, default=3e-4)
    parser.add_argument("--value-warmup-lanes", type=int, default=16)
    parser.add_argument("--expected-temperatures", default="", help="tripwire; empty skips the check")
    parser.add_argument("--preflight-only", action="store_true", help="rescore all loaded rows and exit without warmup, optimizer, or save")
    parser.add_argument("--preflight-max-abs-log-ratio", type=float, default=0.01, help="explicit maximum error tolerance for FP16 observation storage")
    parser.add_argument("--preflight-mean-abs-log-ratio", type=float, default=0.001, help="explicit row-weighted mean error tolerance for FP16 observation storage")
    args = parser.parse_args(argv)
    if args.max_shards < 1:
        parser.error("--max-shards must be positive")
    if args.row_batch_size < 0 or args.input_cache_mib < 0:
        parser.error("row batch size and input cache budget must be nonnegative")

    return args


class UpdateSession:
    """One model/AdamW lifetime; errors poison the session until disk recovery.

    Rollout tensors stay local to _run_update and are released each round.
    Model construction and optimizer restoration happen only on the first round.
    """

    def __init__(self, args):
        self.options = self._options(args)
        self.loaded = load_event_checkpoint(args.checkpoint, device=torch.device(args.device),
                                            initialization_only=getattr(args, "initialization_only", False))
        self.component_config = resolve_config(self.loaded.payload, enabled=args.component_gae,
            gamma=args.gamma, lambdas=dict(outcome=args.outcome_gae_lambda,
                tower=args.tower_gae_lambda, overflow=args.overflow_gae_lambda))
        self.component_critic = load_critic(self.loaded.model, self.loaded.payload, self.component_config)
        self.component_optimizer = None
        if self.component_config is not None and args.value_warmup_steps:
            raise ValueError("component GAE uses per-component PPO targets; scalar value warmup must be disabled")
        # Historical opponents remain in the frozen plan; no anchor is loaded.
        self.reference_model = None
        self.reference_sha = None
        self.shard_cache = None
        self.last_timings = {}
        self.optimizer = None
        self.progress = None
        self.failed = False

    @staticmethod
    def _options(args):
        return {k: v for k, v in vars(args).items() if k not in {"cache_root", "checkpoint", "output"}}

    def update(self, args):
        if self.failed:
            raise RuntimeError("learner session failed; restart from a verified checkpoint")
        try:
            if self._options(args) != self.options:
                raise ValueError("learner options changed during the session")
            return _run_update(args, self)
        except BaseException:
            self.failed = True
            raise


def _run_update(args, session):
    loaded = session.loaded
    model, state = loaded.model, loaded.payload
    reference_model = session.reference_model
    print(json.dumps({"event": "checkpoint-loaded", "checkpoint_sha256": loaded.checkpoint_sha256,
                      "model_contract_hash": loaded.contract_sha256,
                      "snapshot_only": model.backbone.config.snapshot_only}, sort_keys=True), flush=True)

    # Saved legacy controllers must never re-enable the removed anchor.
    beta = 0.0
    timings = {}
    started = time.perf_counter()
    def mark(name):
        nonlocal started
        if next(model.parameters()).is_cuda:
            torch.cuda.synchronize(next(model.parameters()).device)
        now = time.perf_counter()
        timings[name] = now - started
        started = now


    # Workers write into per-worker subdirectories, so discovery is recursive.
    summaries = sorted(args.cache_root.glob("**/part-*.json"))[: args.max_shards]
    if not summaries:
        raise SystemExit(f"no rollout shards under {args.cache_root}")
    expected = (
        tuple(float(item) for item in args.expected_temperatures.split(","))
        if args.expected_temperatures
        else None
    )
    # Mandatory on the exact loaded checkpoint, before even constructing an
    # optimizer. Never replace real-shard scoring with a synthetic fixture.
    counts = {}
    row_metadata = []
    prepared = None
    plan_path = args.cache_root.parent / "match-plan.json"
    match_plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
    mixed = None
    if args.mixed_deck_config is not None:
        mixed = json.loads(args.mixed_deck_config.read_text())
        if (match_plan is None or match_plan.get("version") != "mixed-deck.v1"
                or match_plan.get("start_iteration") != mixed["start_iteration"]
                or match_plan.get("deck_policy") != mixed.get("deck_policy")
                or match_plan.get("reward_config") != mixed.get("reward_config")
                or match_plan.get("allocation_policy") != mixed.get("allocation_policy")
                or match_plan["pool"]["fixed"][0]["sha256"] != mixed["baseline"]["sha256"]
                or match_plan["pool"]["fixed"][1:] != mixed["fixed"]
                or match_plan["action_constraints"] != mixed["action_constraints"]
                or match_plan["iteration"] != state.get(PPO_TRAINING_KEY, {}).get("iterations", 0) + 1
                or args.minibatch_lanes != 0 or args.epochs != 1 or args.lr != 1e-5):
            raise ValueError("mixed-deck learner configuration/plan/baseline mismatch")
        round_number = match_plan["mixed_round"]
        controller = state.get("mixed_deck_controller")
        if round_number != 1:
            if (controller is None or controller["baseline_sha256"] != mixed["baseline"]["sha256"]
                    or controller["mixed_round"] != round_number - 1):
                raise ValueError("mixed-deck controller continuity mismatch")

    elif match_plan is not None and match_plan.get("version") == "mixed-deck.v1":
        raise ValueError("mixed-deck shards require a mixed-deck learner configuration")
    try:
        lanes, counts = load_complete_rollout(
            summaries, expected_checkpoint_sha256=loaded.checkpoint_sha256,
            expected_model_contract_hash=loaded.contract_sha256,
            row_metadata=row_metadata,
            match_plan=match_plan,
            shard_cache=session.shard_cache,
            expected_minimum_delay_ms=MINIMUM_POLICY_DELAY_MS,
        )
        print(json.dumps({"event": "rollout-validation", **counts}, sort_keys=True), flush=True)
        from native_runner.training.event_tower_rewards import validate_reward_gamma
        validate_reward_gamma(counts["reward_config"], args.gamma)
        mark("load_validate_seconds")
        if args.row_batch_size:
            started = time.perf_counter()
            prepared = PreparedEventRollout(
                model, lanes, row_batch_size=args.row_batch_size, minibatch_lanes=args.minibatch_lanes,
                device_cache_bytes=args.input_cache_mib * 1024**2,
                bucket_by_entities=not args.no_entity_buckets)
            print(json.dumps({"event": "rollout-prepared", **prepared.as_dict(),
                              "seconds": time.perf_counter() - started}), flush=True)
        mark("prepare_seconds")
        preflight = preflight_event_rollout(
            model, lanes,
            row_metadata=row_metadata,
            chunk_steps=args.sequence_chunk_steps,
            clip_ratio=args.clip_ratio,
            max_abs_log_ratio=args.preflight_max_abs_log_ratio,
            mean_abs_log_ratio=args.preflight_mean_abs_log_ratio,
            expected_temperatures=expected,
            prepared=prepared,
            component_critic=session.component_critic,
        )
    except (ValueError, TypeError, RuntimeError) as error:
        metrics = error.metrics if isinstance(error, EventPreflightError) else {}
        print(json.dumps({"event": "event-preflight", **counts, **metrics,
                          "passed": False, "error": str(error)}, sort_keys=True, allow_nan=False), flush=True)
        raise SystemExit("event preflight failed; no update or checkpoint save performed") from error
    mark("preflight_seconds")
    print(json.dumps({"event": "event-preflight", **counts, **preflight.as_dict()},
                     sort_keys=True, allow_nan=False), flush=True)
    if args.preflight_only:
        return 0
    shards = counts["shards"]
    rewards = [lane.outcome_reward for lane in lanes]
    margins = [abs(reward) for reward in rewards]

    if session.optimizer is None:
        session.optimizer, session.progress = restore_optimizer(model, state, learning_rate=args.lr)
    optimizer, progress = session.optimizer, session.progress
    if session.component_critic is not None and session.component_optimizer is None:
        session.component_optimizer = restore_component_optimizer(session.component_critic, state,
            learning_rate=optimizer.param_groups[0]["lr"])
    gae_config = (session.component_config.as_dict() if session.component_config is not None else
                  dict(version="event-scalar-gae.v1", gamma=args.gamma, gae_lambda=args.gae_lambda))
    if session.component_config is not None:
        print(json.dumps(dict(event="gae-config", config=gae_config,
            component_critic_resumed=COMPONENT_KEY in state, original_critic_target="outcome",
            auxiliary_parameters=sum(p.numel() for p in session.component_critic.parameters()))), flush=True)
    resumed = PPO_TRAINING_KEY in state
    print(json.dumps({"event": "optimizer-state", "resumed": resumed,
                      "previous_rl_without_optimizer": not resumed and "rl_steps" in state,
                      "learning_rates": [group["lr"] for group in optimizer.param_groups],
                      "value_warmup_skipped": resumed and args.value_warmup_steps > 0,
                      **progress}, sort_keys=True), flush=True)

    # Warm the critic up first: GAE computed from a critic that cannot tell a win
    # from a loss is close to noise, and the first PPO iterations would then move
    # the policy on noise. Only the critic's own parameters are in this optimizer.
    if args.value_warmup_steps > 0 and not resumed:
        if prepared is not None:
            prepared.clear_values()
        critic_optimizer = torch.optim.Adam(
            [parameter for _, parameter in value_head_parameters(model)], lr=args.value_warmup_lr
        )
        warmup = warm_up_value(
            model,
            lanes,
            critic_optimizer,
            steps=args.value_warmup_steps,
            gamma=args.gamma,
            chunk_steps=args.sequence_chunk_steps,
            minibatch_lanes=args.value_warmup_lanes,
        )
        print(
            json.dumps(
                {"event": "value-warmup", **{key: round(value, 6) if isinstance(value, float) else value
                                              for key, value in warmup.as_dict().items()}},
                sort_keys=True,
            ),
            flush=True,
        )

    mark("optimizer_restore_warmup_seconds")
    stats = event_ppo_update(
        model,
        lanes,
        optimizer,
        config=EventPpoConfigV1(
            epochs=args.epochs,
            target_kl=args.target_kl,
            clip_ratio=args.clip_ratio,
            entropy_coefficient=args.entropy_coefficient,
            gamma=args.gamma,
            gae_lambda=args.gae_lambda,
            component_gae=session.component_config,
            minibatch_lanes=args.minibatch_lanes,
            sequence_chunk_steps=args.sequence_chunk_steps,
            kl_coefficient=beta,
            bc_coefficient=args.bc_coefficient,
            bc_margin_threshold=args.bc_margin_threshold,
            deck_weights=None if mixed is None else match_plan["learning_weights"],
            reference_deck_id=None,
        ),
        expected_temperatures=expected,
        reference_model=reference_model,
        prepared=prepared,
        component_critic=session.component_critic,
        component_optimizer=session.component_optimizer,
        phase_timings=timings,
    )
    mark("ppo_seconds")
    if prepared is not None:
        print(json.dumps({"event": "rollout-cache-used", **prepared.as_dict()}), flush=True)

    if stats.optimizer_steps > 0 and any(group["lr"] > 0 for group in optimizer.param_groups):
        from native_runner.training.event_checkpoint import complete_event_initialization
        complete_event_initialization(model)

    next_beta = 0.0
    wins = sum(1 for value in rewards if value > .5)
    print(
        json.dumps(
            {
                "event": "ppo-update",
                "shards": shards,
                "lanes": stats.lanes,
                "rows": stats.steps,
                "terminal_rows": len(rewards),
                "wins": wins,
                "losses": sum(1 for value in rewards if value < -.5),
                "draws": sum(1 for value in rewards if -.5 <= value <= .5),
                "reward_config": counts["reward_config"],
                "gae_config": gae_config,
                "reward_components": counts["reward_components"],
                "mean_training_reward": sum(float(lane.reward.sum()) for lane in lanes)/len(lanes),
                "mean_reward": round(sum(rewards) / max(1, len(rewards)), 4),
                "kl_coefficient": round(beta, 6),
                "kl_coefficient_next": round(next_beta, 6),
                "reference": None if reference_model is None else args.reference_checkpoint.name,
                "mean_margin": round(sum(margins) / max(1, len(margins)), 4),
                **{key: round(value, 6) if isinstance(value, float) else value
                   for key, value in stats.as_dict().items()},
                "target_kl": args.target_kl,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    if args.output is not None:
        extra = {
            "parent_checkpoint_sha256": loaded.checkpoint_sha256,
            "policy_version": f"{args.checkpoint.stem}-ppo",
            "kl_coefficient": next_beta,
            "rl_reference_policy": dict(version="reference-disabled.v1", enabled=False),
            "rl_steps": stats.steps,
            "rl_action_constraints": counts["action_constraints"],
            "rl_execution_credit_contract": counts["execution_credit_contract"],
            "rl_reward_config": counts["reward_config"],
            "rl_gae_config": gae_config,
            "rl_reward_components": counts["reward_components"],
            "rl_execution_counts": {key: counts[key] for key in (
                "rows", "trainable_rows", "excluded_rows", "empty_training_lanes", "execution_status_counts")},
            PPO_TRAINING_KEY: optimizer_checkpoint(model, optimizer, progress, stats),
        }
        if session.component_config is not None:
            extra[COMPONENT_KEY] = component_checkpoint(session.component_critic, session.component_optimizer,
                session.component_config, state, stats)
            extra["rl_component_transition"] = state.get("rl_component_transition", dict(
                parent_checkpoint_sha256=loaded.checkpoint_sha256,
                previous_reward_config=state.get("rl_reward_config"),
                previous_gae_config=state.get("rl_gae_config"),
                initialization="original-critic-to-outcome; zero-output-tower-and-overflow",
                old_optimizer_preserved=True))
        if match_plan is not None:
            extra["rl_mixed_opponents"] = dict(counts["mixed_opponents"],
                actual_training_lanes=counts["training_lanes"], trainable_rows=counts["trainable_rows"],
                excluded_rows=counts["excluded_rows"])
        if mixed is not None:
            extra["mixed_deck_controller"] = dict(baseline_sha256=mixed["baseline"]["sha256"],
                mixed_round=match_plan["mixed_round"], coefficient=next_beta,
                target=0.0, minimum=0.0, maximum=0.0, deck_id=None,
                version="reference-disabled.v1", enabled=False)
        mark("metrics_seconds")
        checksum = save_event_checkpoint(args.output, model, extra=extra)
        session.loaded = replace(loaded, payload=extra, checkpoint_sha256=checksum)
        session.progress = {key: extra[PPO_TRAINING_KEY][key] for key in progress}
    mark("save_seconds")
    session.last_timings = timings
    print(json.dumps(dict(event="update-timings", **timings)), flush=True)
    if args.output is not None:
        print(json.dumps({"event": "saved", "path": str(args.output),
                          "checkpoint_sha256": checksum}), flush=True)
    return 0


def main() -> int:
    args = parse_args()
    return UpdateSession(args).update(args)


if __name__ == "__main__":
    raise SystemExit(main())
