import hashlib
import json
import os
from pathlib import Path
import random

import torch

from ..event_checkpoint import save_event_checkpoint, load_event_checkpoint
from .config import SearchConfig
from .contracts import semantic_digest
from .learner import SearchLearner
from .model import load_checkpoint, save_checkpoint
from .replay import ReplayPool
from .storage import atomic_write, load_episode


SOURCE_SHA = "671f2213401ae95a2464d0ad2d44bd67e00ad85e6331ca33397f06c75764a50b"


def configure_deterministic_training():
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identical_state(first, second):
    left, right = first.state_dict(), second.state_dict()
    return left.keys() == right.keys() and all(
        left[key].dtype == right[key].dtype and left[key].shape == right[key].shape
        and torch.equal(left[key].cpu(), right[key].cpu()) for key in left)


def publish(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True, allow_nan=False).encode(), replace=True)


def export_initial(source, destination, config, *, expected_sha=SOURCE_SHA, seed=20261013):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    source_model, _, source_sha = load_checkpoint(source, expected_sha256=expected_sha)
    if config.particles != 1 or config.root_noise != 0 or config.late_temperature != 1:
        raise ValueError("clean experiment requires one world, zero noise, temperature one")
    marker = destination / "origin.json"
    if marker.exists():
        origin = json.loads(marker.read_text())
        if (origin["source_sha256"] != source_sha or origin["config_sha256"] != config.sha256
                or origin["seed"] != seed or file_sha(destination / "initial.pt") != origin["checkpoint_sha256"]
                or file_sha(destination / "baseline.pt") != origin["baseline_sha256"]):
            raise ValueError("initial export provenance differs")
        restored, payload, _ = load_checkpoint(destination / "initial.pt")
    else:
        random.seed(seed)
        torch.manual_seed(seed)
        learner = SearchLearner(source_model, ReplayPool(), config=config)
        identity = save_checkpoint(destination / "initial.pt", source_model, learner.optimizer, generation=0,
                    parent_sha256=source_sha, config_sha256=config.sha256,
                    replay_state=learner.replay.state_dict(), sample_credit=0, optimizer_steps=0)
        baseline_sha = save_event_checkpoint(destination / "baseline.pt", source_model.policy)
        restored, payload, _ = load_checkpoint(destination / "initial.pt", expected_sha256=identity)
        origin = dict(schema="fair-clean-initial.v1", source_sha256=source_sha, checkpoint_sha256=identity,
                      baseline_sha256=baseline_sha, config_sha256=config.sha256, seed=seed)
    baseline = load_event_checkpoint(destination / "baseline.pt").model
    if (not identical_state(source_model, restored) or not identical_state(source_model.policy, baseline)
            or payload["config_sha256"] != config.sha256 or payload["optimizer"]["state"]
            or payload["optimizer_steps"] or payload["sample_credit"]
            or payload["replay_state"]["episodes"] or payload["replay_state"]["identities"]):
        raise ValueError("clean initial must preserve all weights and reset training state")
    publish(marker, origin)
    return origin


def load_cohort(root, manifest):
    root = Path(root)
    config = SearchConfig(**manifest["config"])
    jobs = {job["episode_id"]: job for job in manifest["jobs"]}
    split = manifest["split"]
    counts = manifest.get("cohort_counts", dict(total=128, train=112, validation=16))
    if (set(counts) != {"total", "train", "validation"}
            or any(type(value) is not int or value <= 0 for value in counts.values())
            or counts["total"] != counts["train"] + counts["validation"]
            or len(jobs) != counts["total"] or len(manifest["jobs"]) != counts["total"] or set(split) != set(jobs)
            or list(split.values()).count("train") != counts["train"]
            or list(split.values()).count("validation") != counts["validation"]
            or config.sha256 != manifest["config_sha256"] or config.root_noise != 0
            or config.late_temperature != 1 or config.particles != 1
            or manifest.get("action_sampling") != "independent-visits.v1"):
        raise ValueError("invalid clean cohort contract")
    episodes, containers = {}, {}
    for identity, job in jobs.items():
        directory = root / "replay" / identity
        receipt = json.loads((directory / "validated.json").read_text())
        episode = load_episode(directory / "completed.json")
        if (episode.episode_id != identity or episode.current_sha256 != manifest["checkpoint_sha256"]
                or episode.generation != job["generation"] or list(episode.owner_sha256) != job["owner_sha256"]
                or receipt["episode_id"] != identity or receipt["attempt"] != episode.attempt
                or receipt["rows"] != len(episode.decisions) or not episode.decisions
                or any(row.config_sha256 != config.sha256 or row.runtime_sha256 != receipt["runtime_sha256"]
                       for row in episode.decisions)):
            raise ValueError("clean episode provenance differs: " + identity)
        episodes[identity] = episode
        containers[identity] = json.loads((directory / "completed.json").read_text())["sha256"]
    train_rows = sum(len(episode.decisions) for identity, episode in episodes.items() if split[identity] == "train")
    steps, remainder = divmod(2 * train_rows, config.batch_size)
    experiment = dict(schema="fair-clean-offline-consumer.v1", seed=20261013,
        initial_sha256=manifest["checkpoint_sha256"], config=manifest["config"], split=split,
        source_sha256=containers, manifest_sha256=semantic_digest(manifest), arrival_order=sorted(jobs),
        steps=steps, sample_remainder=remainder, training_rows=train_rows, sample_budget=2 * train_rows,
        patience=steps + 2, validation_interval=64, selection_metric="policy_ce", target_temperature=1.,
        deterministic_training=True)
    return episodes, experiment
