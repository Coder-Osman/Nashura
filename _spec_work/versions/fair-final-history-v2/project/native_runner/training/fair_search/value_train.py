import argparse
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import random
import time

import torch
from torch import nn
import torch.nn.functional as functional

from .config import SearchConfig
from .contracts import semantic_digest
from .model import load_checkpoint, save_checkpoint
from .replay import ReplayPool
from .storage import atomic_write, torch_bytes
from .value_data import BASE_SHA, load_episode


def policy_digest(model):
    return semantic_digest(model.policy.state_dict())


def fresh_head(template, seed=20261012):
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(seed)
        head = deepcopy(template).cpu()
        for module in head.modules():
            if isinstance(module, nn.Linear):
                module.reset_parameters()
        nn.init.zeros_(head[-1].weight)
        nn.init.zeros_(head[-1].bias)
    return head.float().requires_grad_(True)


def load_cache(path):
    path = Path(path)
    marker = json.loads(path.with_suffix(".json").read_text())
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != marker["sha256"]:
        raise ValueError("feature cache checksum differs")
    cache = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    if cache["schema"] != "fair-value-features.v1" or cache["model_sha256"] != BASE_SHA:
        raise ValueError("feature cache model differs")
    if cache["features"].dtype != torch.float32 or not bool(torch.isfinite(cache["features"]).all()):
        raise ValueError("invalid FP32 features")
    return cache


def cache_episode(model, marker, destination, *, expected_policy_sha, episode=None):
    marker = Path(marker)
    metadata = json.loads(marker.read_text())
    destination = Path(destination)
    if destination.exists() and destination.with_suffix(".json").exists():
        cache = load_cache(destination)
        if (cache["source_sha256"] != metadata["semantic_sha256"]
                or cache["policy_sha256"] != expected_policy_sha):
            raise ValueError("feature cache provenance differs")
        return cache
    episode = load_episode(marker) if episode is None else episode
    if episode.model_sha256 != BASE_SHA:
        raise ValueError("cache producing policy differs")
    features, baseline, semantics = [], [], []
    with torch.inference_mode():
        for row in episode.decisions:
            context, logits = model.encode(row.frame)
            features.append(context.value_context.detach().cpu().float())
            baseline.append(logits.detach().cpu().float())
            semantics.append(semantic_digest(row.frame))
        rng = random.Random(int(hashlib.sha256(episode.episode_id.encode()).hexdigest()[:16], 16))
        for index in rng.sample(range(len(features)), min(3, len(features))):
            context, logits = model.encode(episode.decisions[index].frame)
            torch.testing.assert_close(context.value_context.cpu(), features[index], rtol=1e-5, atol=1e-5)
            torch.testing.assert_close(logits.cpu(), baseline[index], rtol=1e-5, atol=1e-5)
    cache = dict(schema="fair-value-features.v1", episode_id=episode.episode_id,
                 split=episode.split, model_sha256=BASE_SHA, policy_sha256=expected_policy_sha,
                 source_sha256=metadata["semantic_sha256"], input_sha256=semantics,
                 features=torch.cat(features), baseline=torch.cat(baseline),
                 labels=torch.tensor([row.label for row in episode.decisions], dtype=torch.long),
                 owners=torch.tensor([row.owner for row in episode.decisions], dtype=torch.long),
                 ticks=torch.tensor([row.tick for row in episode.decisions], dtype=torch.long))
    data = torch_bytes(cache)
    atomic_write(destination, data, replace=True)
    atomic_write(destination.with_suffix(".json"), json.dumps(dict(
        sha256=hashlib.sha256(data).hexdigest(), source_sha256=cache["source_sha256"],
        model_sha256=BASE_SHA, policy_sha256=expected_policy_sha, rows=len(features))).encode(), replace=True)
    return cache


class ValueTrainer:
    def __init__(self, head, episodes, *, dataset_sha256, policy_sha256, device="cpu", batch_size=256):
        if not episodes or any(episode["split"] != "train" for episode in episodes):
            raise ValueError("value optimization requires training split only")
        self.head = head.to(device).train()
        self.episodes = episodes
        self.dataset_sha256, self.policy_sha256 = dataset_sha256, policy_sha256
        self.device, self.batch_size = device, batch_size
        self.optimizer = torch.optim.AdamW(self.head.parameters(), lr=1e-4, weight_decay=1e-4)
        self.rng = random.Random(20261012)
        self.steps = 0
        self.epoch = 0
        self.step_in_epoch = 0
        self.steps_per_epoch = math.ceil(sum(len(episode["labels"]) for episode in episodes) / batch_size)
        self.best_ce = float("inf")
        self.stale = 0

    def step(self):
        examples = []
        for _ in range(self.batch_size):
            episode = self.rng.choice(self.episodes)
            index = self.rng.randrange(len(episode["labels"]))
            examples.append((episode["features"][index], episode["labels"][index]))
        features = torch.stack([item[0] for item in examples]).to(self.device)
        labels = torch.stack([item[1] for item in examples]).to(self.device)
        self.optimizer.zero_grad(set_to_none=True)
        loss = functional.cross_entropy(self.head(features), labels)
        if not bool(torch.isfinite(loss)):
            raise ValueError("nonfinite value loss")
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.head.parameters(), 1., error_if_nonfinite=True)
        self.optimizer.step()
        self.steps += 1
        self.step_in_epoch += 1
        return float(loss.detach())

    def save(self, path):
        payload = dict(schema="fair-value-checkpoint.v1", base_sha256=BASE_SHA,
                       dataset_sha256=self.dataset_sha256, policy_sha256=self.policy_sha256,
                       head=self.head.state_dict(), optimizer=self.optimizer.state_dict(),
                       random=self.rng.getstate(), torch_rng=torch.get_rng_state(),
                       steps=self.steps, epoch=self.epoch, step_in_epoch=self.step_in_epoch,
                       steps_per_epoch=self.steps_per_epoch, batch_size=self.batch_size,
                       best_ce=self.best_ce, stale=self.stale)
        atomic_write(path, torch_bytes(payload), replace=True)

    def resume(self, path):
        state = torch.load(path, map_location="cpu", weights_only=True)
        if (state["schema"] != "fair-value-checkpoint.v1" or state["base_sha256"] != BASE_SHA
                or state["dataset_sha256"] != self.dataset_sha256
                or state["policy_sha256"] != self.policy_sha256
                or state["steps_per_epoch"] != self.steps_per_epoch or state["batch_size"] != self.batch_size):
            raise ValueError("value checkpoint provenance differs")
        self.head.load_state_dict(state["head"])
        self.optimizer.load_state_dict(state["optimizer"])
        self.rng.setstate(state["random"])
        torch.set_rng_state(state["torch_rng"])
        for name in ("steps", "epoch", "step_in_epoch", "best_ce", "stale"):
            setattr(self, name, state[name])


def episode_metrics(logits, episode, mask=None):
    mask = torch.ones(len(logits), dtype=torch.bool) if mask is None else mask
    if not bool(mask.any()):
        return None
    logits = logits[mask].float()
    labels = episode["labels"][mask]
    probabilities = logits.softmax(-1)
    targets = functional.one_hot(labels, 3).float()
    return dict(rows=int(mask.sum()), ce=float(functional.cross_entropy(logits, labels)),
                brier=float(((probabilities - targets)**2).sum(-1).mean()),
                predicted_draw=float(probabilities[:, 1].mean()),
                actual_draw=float((labels == 1).float().mean()))


def evaluate(head, episodes):
    if not episodes:
        raise ValueError("empty evaluation split")
    device = next(head.parameters()).device
    groups = {name: [] for name in ("all", "owner0", "owner1", "early", "middle", "late")}
    calibration = {}
    comparisons = []
    with torch.no_grad():
        for episode in episodes:
            logits = torch.cat([head(batch.to(device)).cpu() for batch in episode["features"].split(1024)])
            masks = dict(all=None, owner0=episode["owners"] == 0, owner1=episode["owners"] == 1,
                         early=episode["ticks"] < 1200,
                         middle=(episode["ticks"] >= 1200) & (episode["ticks"] < 3600),
                         late=episode["ticks"] >= 3600)
            for name, mask in masks.items():
                candidate = episode_metrics(logits, episode, mask)
                if candidate is not None:
                    groups[name].append(dict(candidate=candidate,
                                             baseline=episode_metrics(episode["baseline"], episode, mask)))
            comparisons.append(dict(episode_id=episode["episode_id"], **groups["all"][-1]))
            for source, values in (("candidate", logits), ("baseline", episode["baseline"])):
                probabilities = values.softmax(-1)
                for category, name in enumerate(("win", "draw", "loss")):
                    key = source + "_" + name
                    bins = calibration.setdefault(key, [dict(weight=0., probability=0., observed=0.) for _ in range(10)])
                    indices = (probabilities[:, category] * 10).long().clamp(max=9)
                    for index, cell in enumerate(bins):
                        selected = indices == index
                        weight = float(selected.sum()) / len(indices) / len(episodes)
                        if weight:
                            cell["weight"] += weight
                            cell["probability"] += weight * float(probabilities[selected, category].mean())
                            cell["observed"] += weight * float((episode["labels"][selected] == category).float().mean())
    aggregate = {}
    for name, rows in groups.items():
        aggregate[name] = dict(episodes=len(rows), **{
            source: {metric: sum(row[source][metric] for row in rows) / len(rows)
                     for metric in ("ce", "brier", "predicted_draw", "actual_draw")}
            for source in ("candidate", "baseline")}) if rows else dict(episodes=0)
    calibration = {key: [{**cell, "probability": cell["probability"] / cell["weight"] if cell["weight"] else None,
                         "observed": cell["observed"] / cell["weight"] if cell["weight"] else None}
                        for cell in cells] for key, cells in calibration.items() if "_" in key}
    return dict(groups=aggregate, calibration=calibration, episodes=comparisons)


def acceptance(report, resamples=10000):
    differences = [row["candidate"]["ce"] - row["baseline"]["ce"] for row in report["episodes"]]
    rng = random.Random(20261012)
    bootstrapped = sorted(sum(rng.choices(differences, k=len(differences))) / len(differences)
                          for _ in range(resamples))
    lower, upper = bootstrapped[int(.025 * resamples)], bootstrapped[min(resamples - 1, int(.975 * resamples))]
    metrics = report["groups"]["all"]
    return dict(passed=upper < 0 and metrics["candidate"]["brier"] <= metrics["baseline"]["brier"],
                ce_difference=sum(differences) / len(differences), lower95=lower, upper95=upper,
                resamples=resamples, unit="complete-episode")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", default="cuda:7")
    args = parser.parse_args(argv)
    root = args.root
    torch.set_num_threads(1)
    torch.manual_seed(20261012)
    manifest = json.loads((root / "manifest.json").read_text())
    model, _, _ = load_checkpoint(root / "initial.pt", device=args.device, expected_sha256=BASE_SHA)
    model.eval().requires_grad_(False)
    policy_sha = policy_digest(model)
    dataset_sha = semantic_digest(manifest)
    training = root / "training"
    training.mkdir(exist_ok=True)
    parts = {name: [] for name in ("train", "validation", "test")}
    for job in manifest["jobs"]:
        marker = root / "episodes" / job["episode_id"] / "completed.json"
        destination = root / "features" / (job["episode_id"] + ".pt")
        receipt_path = destination.with_suffix(".json")
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if receipt.get("schema") == "fair-value-feature-manifest.v1":
            source = json.loads(marker.read_text())
            if (receipt.get("seed") != job["seed"] or receipt.get("split") != job["split"]
                    or receipt.get("runtime_sha256") != manifest["runtime_sha256"]
                    or receipt.get("source_sha256") != source["semantic_sha256"]):
                raise ValueError("validated feature dataset differs")
            cache = cache_episode(model, marker, destination, expected_policy_sha=policy_sha)
        else:
            episode = load_episode(marker)
            if (episode.seed != job["seed"] or episode.split != job["split"]
                    or episode.runtime_sha256 != manifest["runtime_sha256"]):
                raise ValueError("frozen dataset differs")
            cache = cache_episode(model, marker, destination, expected_policy_sha=policy_sha, episode=episode)
        parts[job["split"]].append(cache)
        atomic_write(training / "progress.json", json.dumps(dict(stage="features", count=sum(map(len, parts.values())))).encode(), replace=True)
    if {name: len(rows) for name, rows in parts.items()} != dict(train=3072, validation=512, test=512):
        raise ValueError("full frozen 4096-episode dataset required")
    source_identities = [(episode["episode_id"], episode["source_sha256"])
                         for episodes in parts.values() for episode in episodes]
    dataset_sha = semantic_digest((dataset_sha, source_identities))
    trainer = ValueTrainer(fresh_head(model.wdl_head), parts["train"], dataset_sha256=dataset_sha,
                           policy_sha256=policy_sha, device=args.device)
    latest, best = training / "latest.pt", training / "best.pt"
    if latest.exists():
        trainer.resume(latest)
    while trainer.epoch < 20 and trainer.stale < 5:
        started = time.monotonic()
        while trainer.step_in_epoch < trainer.steps_per_epoch:
            loss = trainer.step()
            if trainer.steps % 100 == 0:
                trainer.save(latest)
                atomic_write(training / "progress.json", json.dumps(dict(stage="training", epoch=trainer.epoch,
                             steps=trainer.steps, loss=loss)).encode(), replace=True)
        report = evaluate(trainer.head, parts["validation"])
        score = report["groups"]["all"]["candidate"]["ce"]
        trainer.epoch += 1
        trainer.step_in_epoch = 0
        improved = score < trainer.best_ce
        trainer.stale = 0 if improved else trainer.stale + 1
        if improved:
            trainer.best_ce = score
            trainer.save(best)
        if policy_digest(model) != policy_sha:
            raise ValueError("frozen policy changed")
        trainer.save(latest)
        atomic_write(training / f"epoch-{trainer.epoch:02d}.json", json.dumps(dict(
            epoch=trainer.epoch, steps=trainer.steps, seconds=time.monotonic() - started,
            validation=report)).encode(), replace=True)
        print(json.dumps(dict(epoch=trainer.epoch, validation_ce=score, best_ce=trainer.best_ce)), flush=True)
    trainer.resume(best)
    report = evaluate(trainer.head, parts["test"])
    report["acceptance"] = acceptance(report)
    report.update(dataset_sha256=dataset_sha, policy_sha256=policy_sha, base_sha256=BASE_SHA,
                  best_epoch=trainer.epoch, optimizer_steps=trainer.steps)
    model.wdl_head.load_state_dict(trainer.head.state_dict())
    if policy_digest(model) != policy_sha:
        raise ValueError("export changed frozen policy")
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
    candidate_sha = save_checkpoint(training / "candidate.pt", model, optimizer, generation=0,
                                    parent_sha256=BASE_SHA, config_sha256=SearchConfig(particles=1).sha256,
                                    replay_state=ReplayPool().state_dict(), sample_credit=0,
                                    optimizer_steps=0, replace=True)
    report["candidate_sha256"] = candidate_sha
    atomic_write(training / "report.json", json.dumps(report).encode(), replace=True)
    atomic_write(training / "done.json", json.dumps(dict(candidate_sha256=candidate_sha,
                 acceptance=report["acceptance"], source="random-head-terminal-supervision",
                 dataset_sha256=dataset_sha, policy_sha256=policy_sha)).encode(), replace=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
