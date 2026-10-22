"""Train an isolated learner variant from already validated FAIR episodes."""

import argparse
import hashlib
import json
from pathlib import Path

import torch

from native_runner.training.event_checkpoint import save_event_checkpoint
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.learner import SearchLearner
from native_runner.training.fair_search.model import load_checkpoint, restore_rng
from native_runner.training.fair_search.replay import ReplayPool
from native_runner.training.fair_search.storage import atomic_write, load_episode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda:1")
    parser.add_argument("--episodes", type=int, default=48)
    parser.add_argument("--steps", type=int, default=24)
    parser.add_argument("--prior-fraction", type=float, default=0.)
    parser.add_argument("--prior-samples", type=int, default=8)
    parser.add_argument("--detach-value-features", action="store_true")
    args = parser.parse_args()
    if args.episodes < 1 or args.steps < 1 or not 0 <= args.prior_fraction <= 1:
        parser.error("invalid episode, step, or prior fraction")
    torch.set_num_threads(1)
    manifest = json.loads((args.root / "manifest.json").read_text())
    config = SearchConfig(**manifest["config"])
    source = args.root / "initial.pt"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_sha != manifest["checkpoint_sha256"]:
        raise ValueError("initial checkpoint differs from generation manifest")
    model, payload, _ = load_checkpoint(source, device=args.device, resume=True,
                                        expected_sha256=source_sha)
    teacher = None
    if args.prior_fraction:
        teacher, _, _ = load_checkpoint(source, device=args.device, expected_sha256=source_sha)
        restore_rng(payload["rng"])
    learner = SearchLearner(model, ReplayPool(max_generations=config.replay_generations,
                                             max_rows=config.replay_rows), config=config,
                            teacher=teacher, teacher_sha256=source_sha,
                            prior_fraction=args.prior_fraction,
                            prior_samples=args.prior_samples,
                            detach_value_features=args.detach_value_features)
    learner.resume(payload)
    selected = []
    for job in manifest["jobs"]:
        directory = args.root / "replay" / job["episode_id"]
        if not (directory / "validated.json").exists():
            continue
        episode = load_episode(directory / "completed.json")
        if (episode.episode_id != job["episode_id"] or episode.current_sha256 != source_sha
                or any(row.config_sha256 != config.sha256 for row in episode.decisions)):
            raise ValueError("episode does not match frozen generation")
        learner.ingest(episode)
        selected.append(episode.episode_id)
        if len(selected) == args.episodes:
            break
    if len(selected) != args.episodes:
        raise ValueError("not enough validated complete episodes")
    metrics = learner.train_available(max_steps=args.steps)
    if len(metrics) != args.steps:
        raise ValueError("insufficient training credit for requested steps")
    args.output.mkdir(parents=True, exist_ok=True)
    policy_path = args.output / "policy.pt"
    if policy_path.exists():
        raise FileExistsError(policy_path)
    policy_sha = save_event_checkpoint(policy_path, model.policy)
    report = dict(schema="fair-learner-comparison.v1", source_sha256=source_sha,
                  policy_sha256=policy_sha, episodes=selected, steps=args.steps,
                  prior_fraction=args.prior_fraction, prior_samples=args.prior_samples,
                  detach_value_features=args.detach_value_features, metrics=metrics)
    atomic_write(args.output / "report.json", json.dumps(report, allow_nan=False).encode())
    print(json.dumps({key: value for key, value in report.items() if key not in ("episodes", "metrics")},
                     sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
