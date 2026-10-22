"""Compare isolated teacher consumers with a frozen whole-episode validation split."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time

import torch

from native_runner.training.event_checkpoint import save_event_checkpoint
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.consumer import EpisodeSampler, evaluate_episodes, split_episodes
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.learner import SearchLearner
from native_runner.training.fair_search.model import load_checkpoint, restore_rng, rng_state
from native_runner.training.fair_search.storage import atomic_write, load_episode, torch_bytes


def publish(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True, allow_nan=False).encode(), replace=True)


def fit_variant(initial, episodes, experiment, output, *, mode, device):
    output.mkdir(parents=True, exist_ok=True)
    identity = semantic_digest(experiment)
    if (output / "done.json").exists():
        result = json.loads((output / "done.json").read_text())
        if result["experiment_sha256"] != identity or result["mode"] != mode:
            raise ValueError("completed consumer experiment differs")
        for name in ("best.pt", "policy.pt"):
            if hashlib.sha256((output / name).read_bytes()).hexdigest() != result[name + "_sha256"]:
                raise ValueError("completed consumer artifact differs")
        return result
    model, initial_payload, _ = load_checkpoint(initial, device=device,
                                                expected_sha256=experiment["initial_sha256"])
    restore_rng(initial_payload["rng"])
    sampler = EpisodeSampler(balanced=mode == "balanced", seed=experiment["seed"])
    train_ids = [key for key in experiment["arrival_order"] if experiment["split"][key] == "train"]
    validation = [episodes[key] for key in experiment["split"] if experiment["split"][key] == "validation"]
    config = SearchConfig(**experiment["config"])
    learner = SearchLearner(model, sampler, config=config,
                            target_temperature=experiment.get("target_temperature", 1.))
    if experiment.get("resume_initial_optimizer", False):
        learner.optimizer.load_state_dict(initial_payload["optimizer"])
    remainder = experiment.get("sample_remainder", 0)
    if type(remainder) is not int or not 0 <= remainder < config.batch_size:
        raise ValueError("invalid sample budget remainder")
    learner.sample_credit = experiment["steps"] * config.batch_size + remainder
    history, best, stale = [], None, 0
    latest = output / "latest.pt"
    saved = torch.load(latest, map_location="cpu", weights_only=True) if latest.exists() else None
    if saved is not None:
        if (saved.get("schema") != "fair-consumer-experiment.v1" or saved["experiment_sha256"] != identity
                or saved["mode"] != mode):
            raise ValueError("consumer checkpoint provenance differs")
        active = saved["sampler"]["identities"]
        if any(key not in train_ids for key in active):
            raise ValueError("validation episode leaked into consumer sampler")
        for key in active:
            sampler.add(episodes[key])
        sampler.load_state_dict(saved["sampler"])
        learner.model.load_state_dict(saved["model"], strict=True)
        learner.optimizer.load_state_dict(saved["optimizer"])
        learner.optimizer_steps = saved["step"]
        learner.sample_credit = saved["sample_credit"]
        if (not 0 <= learner.optimizer_steps <= experiment["steps"]
                or learner.sample_credit != (experiment["steps"] - learner.optimizer_steps) * config.batch_size + remainder):
            raise ValueError("consumer update cursor differs")
        history, best, stale = saved["history"], saved["best"], saved["stale"]
        restore_rng(saved["rng"])
    elif mode != "stream":
        for key in train_ids:
            sampler.add(episodes[key])

    def checkpoint():
        return dict(schema="fair-consumer-experiment.v1", experiment_sha256=identity, mode=mode,
                    model=model.state_dict(), optimizer=learner.optimizer.state_dict(), rng=rng_state(),
                    sampler=sampler.state_dict(), step=learner.optimizer_steps,
                    sample_credit=learner.sample_credit, history=history, best=best, stale=stale)

    def validate():
        nonlocal best, stale
        started = time.perf_counter()
        metrics = evaluate_episodes(model, validation)
        history.append(dict(step=learner.optimizer_steps, validation=metrics,
                            seconds=time.perf_counter() - started))
        selection_metric = experiment.get("selection_metric", "policy_ce")
        score = metrics["equal_episode"][selection_metric]
        improved = best is None or score < best[selection_metric] - 1e-6
        if improved:
            best = dict(step=learner.optimizer_steps, policy_ce=metrics["equal_episode"]["policy_ce"],
                        top_action_ce=metrics["equal_episode"]["top_action_ce"])
            stale = 0
        else:
            stale += 1
        data = torch_bytes(checkpoint())
        if improved:
            atomic_write(output / "best.pt", data, replace=True)
        atomic_write(latest, data, replace=True)
        publish(output / "validation.json", dict(mode=mode, history=history, best=best, stale=stale))
        print(json.dumps(dict(mode=mode, stage="validated", step=learner.optimizer_steps,
                              metrics=metrics["equal_episode"], best=best)), flush=True)

    if not history:
        validate()
    while learner.optimizer_steps < experiment["steps"] and stale < experiment["patience"]:
        if mode == "stream":
            count = experiment["arrival_counts"][learner.optimizer_steps]
            for key in experiment["arrival_order"][:count]:
                if experiment["split"][key] == "train" and key not in sampler.episodes:
                    sampler.add(episodes[key])
        metrics = learner.train_available(max_steps=1)[0]
        publish(output / "status.json", dict(stage="training", mode=mode, metrics=metrics,
                planned_steps=experiment["steps"], best=best, active_episodes=len(sampler.episodes), time=time.time()))
        if learner.optimizer_steps % experiment["validation_interval"] == 0 or learner.optimizer_steps == experiment["steps"]:
            validate()
        elif learner.optimizer_steps % 16 == 0:
            atomic_write(latest, torch_bytes(checkpoint()), replace=True)
    atomic_write(output / "final.pt", torch_bytes(checkpoint()), replace=True)
    final_policy_sha = save_event_checkpoint(output / "final-policy.pt", model.policy)
    best_state = torch.load(output / "best.pt", map_location="cpu", weights_only=True)
    model.load_state_dict(best_state["model"], strict=True)
    policy_path = output / "policy.pt"
    policy_sha = save_event_checkpoint(policy_path, model.policy)
    result = dict(schema="fair-consumer-result.v1", experiment_sha256=identity, mode=mode,
                  best=best, steps=learner.optimizer_steps, promoted=False,
                  early_stopped=stale >= experiment["patience"], coverage=sampler.coverage(),
                  target_temperature=experiment.get("target_temperature", 1.),
                  sample_remainder=learner.sample_credit,
                  final_checkpoint_sha256=hashlib.sha256((output / "final.pt").read_bytes()).hexdigest(),
                  final_policy_sha256=final_policy_sha,
                  history=history, **{"policy.pt_sha256": policy_sha,
                  "best.pt_sha256": hashlib.sha256((output / "best.pt").read_bytes()).hexdigest()})
    publish(output / "done.json", result)
    publish(output / "status.json", dict(stage="awaiting-independent-evaluation", mode=mode,
            best=best, steps=learner.optimizer_steps, promoted=False, time=time.time()))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda:7")
    parser.add_argument("--mode", choices=("all", "stream", "uniform", "balanced"), default="all")
    parser.add_argument("--validation-count", type=int, default=16)
    parser.add_argument("--validation-interval", type=int, default=64)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--load-workers", type=int, default=4)
    parser.add_argument("--target-temperatures", type=float, nargs="+")
    parser.add_argument("--selection-metric", choices=("policy_ce", "top_action_ce"), default="policy_ce")
    args = parser.parse_args()
    if min(args.validation_interval, args.patience, args.load_workers) < 1:
        parser.error("positive intervals, patience and worker count required")
    if args.target_temperatures is not None:
        from native_runner.training.fair_search.learner import visit_target

        if args.mode != "balanced" or len(set(args.target_temperatures)) != len(args.target_temperatures):
            parser.error("temperature experiments require balanced sampling and distinct temperatures")
        for temperature in args.target_temperatures:
            visit_target({"wait": 32}, temperature)
    torch.set_num_threads(1)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((args.root / "manifest.json").read_text())
    ready = json.loads((args.root / "candidate-ready.json").read_text())
    candidate_bytes = Path(ready["candidate"]).read_bytes()
    if hashlib.sha256(candidate_bytes).hexdigest() != ready["sha256"]:
        raise ValueError("arrival audit checkpoint differs")
    candidate = torch.load(Path(ready["candidate"]), map_location="cpu", weights_only=True)
    arrival_order = [row["episode_id"] for row in candidate["replay_state"]["episodes"]]
    jobs = {job["episode_id"]: job for job in manifest["jobs"]}
    if len(arrival_order) != len(jobs) or set(arrival_order) != set(jobs):
        raise ValueError("consumer requires the full validated source cohort")
    logs = [json.loads(line) for line in (args.root / "learning.log").read_text().splitlines()
            if line.startswith("{")]
    steps = {row["step"]: row for row in logs}
    if sorted(steps) != list(range(1, candidate["optimizer_steps"] + 1)):
        raise ValueError("source update log is incomplete")
    split = split_episodes(arrival_order, validation_count=args.validation_count, seed=20261013)
    if not any(split[key] == "train" for key in arrival_order[:steps[1]["ingested"]]):
        raise ValueError("source stream starts with validation-only data")
    containers = {key: json.loads((args.root / "replay" / key / "completed.json").read_text()) for key in arrival_order}
    experiment = dict(schema="fair-consumer-comparison.v1", seed=20261013, split=split,
                    initial_sha256=manifest["checkpoint_sha256"], config=manifest["config"],
                    source_sha256={key: row["sha256"] for key, row in containers.items()},
                    arrival_order=arrival_order, arrival_counts=[steps[step]["ingested"] for step in sorted(steps)],
                    steps=candidate["optimizer_steps"], validation_interval=args.validation_interval,
                    patience=args.patience, selection="equal-episode-validation-" + args.selection_metric,
                    selection_metric=args.selection_metric, target_temperatures=args.target_temperatures,
                    code_sha256={path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in
                        (Path(__file__), Path(__import__(EpisodeSampler.__module__, fromlist=["__file__"]).__file__),
                         Path(__import__(SearchLearner.__module__, fromlist=["__file__"]).__file__))})
    plan = args.output / "experiment.json"
    if plan.exists() and json.loads(plan.read_text()) != experiment:
        raise ValueError("frozen consumer experiment differs")
    if not plan.exists():
        publish(plan, experiment)

    def load(identity):
        directory = args.root / "replay" / identity
        receipt = json.loads((directory / "validated.json").read_text())
        episode = load_episode(directory / "completed.json")
        job = jobs[identity]
        if (episode.episode_id != identity or episode.current_sha256 != experiment["initial_sha256"]
                or episode.generation != job["generation"] or list(episode.owner_sha256) != job["owner_sha256"]
                or receipt["episode_id"] != identity or receipt["rows"] != len(episode.decisions)
                or receipt["attempt"] != episode.attempt
                or any(row.config_sha256 != manifest["config_sha256"] or row.runtime_sha256 != receipt["runtime_sha256"]
                       for row in episode.decisions)):
            raise ValueError("consumer episode provenance differs")
        return episode

    episodes = {}
    with ThreadPoolExecutor(max_workers=args.load_workers) as pool:
        for episode in pool.map(load, arrival_order):
            episodes[episode.episode_id] = episode
            publish(args.output / "status.json", dict(stage="loading", loaded=len(episodes), required=len(jobs), time=time.time()))
            print(json.dumps(dict(stage="loaded", episodes=len(episodes), episode_id=episode.episode_id)), flush=True)
    del candidate, candidate_bytes
    results = {}
    modes = ("stream", "uniform", "balanced") if args.mode == "all" else (args.mode,)
    variants = [(mode, mode, experiment) for mode in modes]
    if args.target_temperatures is not None:
        variants = [(f"temperature-{temperature:g}", "balanced", dict(experiment, target_temperature=temperature))
                    for temperature in args.target_temperatures]
    for name, mode, variant in variants:
        publish(args.output / "status.json", dict(stage="fitting", mode=mode, variant=name, time=time.time()))
        results[name] = fit_variant(args.root / "initial.pt", episodes, variant, args.output / name,
                                    mode=mode, device=args.device)
        publish(args.output / "comparison.json", results)
    publish(args.output / "status.json", dict(stage="completed", modes=list(results), promoted=False, time=time.time()))


if __name__ == "__main__":
    main()
