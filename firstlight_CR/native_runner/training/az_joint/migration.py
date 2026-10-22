import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import random

import torch

from ..fair_search.contracts import semantic_digest
from ..fair_search.storage import atomic_write
from .checkpoint import load_checkpoint, new_optimizer, resume_checkpoint, save_checkpoint
from .config import JointConfig
from .learning import EpisodeCache, EpisodeReference, JointSampler, evaluate, update_batch
from .model import JointPolicyValue
from .source import ASourceAdapter, parameter_digest


STAGES = {"frozen": (True, 2), "unfrozen": (False, 10),
          "supplement-1": (False, 5), "supplement-2": (False, 5)}


def write_json(path, value, *, replace=True):
    atomic_write(path, json.dumps(value, sort_keys=True, allow_nan=False, indent=2).encode(), replace=replace)


def initialize(source, destination, *, config=None, device="cpu"):
    config = config or JointConfig()
    random.seed(config.seed)
    torch.manual_seed(config.seed)
    adapter = ASourceAdapter.load(source, device=device)
    model = JointPolicyValue.from_source(adapter.model).to(device)
    provenance = dict(schema="joint-migration-provenance.v1", source=adapter.manifest,
                      adapter_sha256=adapter.sha256, parameter_mapping=model.migration_mapping,
                      copied_encoder_sha256=parameter_digest(model.encoder.state_dict()))
    identity = save_checkpoint(destination, model, new_optimizer(model, config), config=config,
        state=dict(stage="initialized", step=0), provenance=provenance, migration=True)
    return dict(checkpoint_sha256=identity, provenance=provenance)


class JointMigrationTrainer:
    def __init__(self, model, optimizer, *, config, episodes, stage, provenance):
        if stage not in STAGES:
            raise ValueError("unknown migration stage")
        self.model, self.optimizer, self.config = model, optimizer, config
        self.stage, self.provenance = stage, provenance
        self.train = [episode for episode in episodes if episode.split == "train"]
        self.validation = [episode for episode in episodes if episode.split == "validation"]
        if (not self.train or not self.validation or any(episode.split == "test" for episode in episodes)
                or len({episode.episode_id for episode in episodes}) != len(episodes)):
            raise ValueError("migration fitting needs disjoint train/validation episodes; test is sealed")
        self.data_identity = semantic_digest([(episode.episode_id, episode.identity) for episode in episodes])
        self.sampler = JointSampler(seed=config.seed)
        for episode in self.train:
            self.sampler.add(episode)
        self.updates_per_epoch = math.ceil(sum(len(episode.decisions) for episode in self.train) / config.batch_size)
        self.state = dict(stage=stage, step=0, epoch=0, epoch_step=0, stale=0, best_loss=None,
                          data_sha256=self.data_identity, best_sha256=None)

    def training_mode(self):
        frozen = STAGES[self.stage][0]
        self.model.train()
        self.model.encoder.requires_grad_(not frozen)
        if frozen:
            self.model.encoder.eval()

    def restore(self, state):
        if state["stage"] != self.stage or state["data_sha256"] != self.data_identity:
            raise ValueError("migration stage or data identity changed on resume")
        self.sampler.load_state_dict(state["sampler"])
        self.state = {name: value for name, value in state.items() if name != "sampler"}

    def save(self, path, *, replace=True):
        return save_checkpoint(path, self.model, self.optimizer, config=self.config,
            state={**self.state, "sampler": self.sampler.state_dict()}, provenance=self.provenance,
            migration=True, replace=replace)

    def validate(self):
        return evaluate(self.model, self.validation, migration=True, batch_size=self.config.micro_batch)

    def run(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if self.state["best_loss"] is None:
            report = self.validate()
            self.state["best_loss"] = report["equal_episode"]["total_loss"]
            self.state["best_sha256"] = self.save(directory / "best.pt")
            write_json(directory / "validation-0000.json", report)
        while self.state["epoch"] < STAGES[self.stage][1] and self.state["stale"] < 3:
            self.training_mode()
            while self.state["epoch_step"] < self.updates_per_epoch:
                metrics = update_batch(self.model, self.optimizer, self.sampler, self.config, migration=True)
                self.state["step"] += 1
                self.state["epoch_step"] += 1
                if self.state["step"] % 64 == 0:
                    self.save(directory / "resume.pt")
                write_json(directory / "progress.json", {**self.state, **metrics})
            report = self.validate()
            self.state["epoch"] += 1
            self.state["epoch_step"] = 0
            loss = report["equal_episode"]["total_loss"]
            if loss < self.state["best_loss"] - self.config.validation_improvement:
                self.state.update(best_loss=loss, stale=0)
                self.state["best_sha256"] = self.save(directory / "best.pt")
            else:
                self.state["stale"] += 1
            write_json(directory / f"validation-{self.state['epoch']:04d}.json", report)
            self.save(directory / "resume.pt")
        identity = self.save(directory / "final.pt")
        report = dict(stage=self.stage, state=self.state, coverage=self.sampler.coverage(),
                      best_sha256=hashlib.sha256((directory / "best.pt").read_bytes()).hexdigest(),
                      final_sha256=identity)
        write_json(directory / "completed.json", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("initialize")
    init.add_argument("--source", required=True, type=Path)
    init.add_argument("--output", required=True, type=Path)
    fit = commands.add_parser("fit")
    fit.add_argument("--parent", required=True, type=Path)
    fit.add_argument("--stage", choices=STAGES, required=True)
    fit.add_argument("--episodes", required=True, type=Path)
    fit.add_argument("--output", required=True, type=Path)
    for command in (init, fit):
        command.add_argument("--device", default="cuda:0")
        command.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    config = JointConfig.load(args.config) if args.config else JointConfig()
    if args.command == "initialize":
        print(json.dumps(initialize(args.source, args.output, config=config, device=args.device)))
        return
    catalog = json.loads(args.episodes.read_text())
    cache = EpisodeCache(migration=True)
    episodes = [EpisodeReference(marker, cache) for marker in catalog["markers"]]
    if catalog["schema"] != "joint-migration-fit-data.v1":
        raise ValueError("migration fit manifest version differs")
    resume = args.output / "resume.pt"
    model, optimizer, payload, identity = resume_checkpoint(resume if resume.exists() else args.parent,
        migration=True, device=args.device, config=config)
    trainer = JointMigrationTrainer(model, optimizer, config=config, episodes=episodes,
                                    stage=args.stage, provenance=payload["provenance"])
    if resume.exists():
        trainer.restore(payload["state"])
    print(json.dumps(trainer.run(args.output)))


if __name__ == "__main__":
    main()
