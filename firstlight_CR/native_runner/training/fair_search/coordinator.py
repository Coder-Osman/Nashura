from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random

from .config import SearchConfig
from .contracts import require_sha, semantic_digest
from .model import load_checkpoint, save_checkpoint
from .storage import atomic_write, load_episode


HOST_ROLES = {"34": "learner-evaluator", "35": "producer", "37": "producer", "39": "producer", "41": "producer"}


def generation_jobs(generation, current_sha256, history, *, seed=550001, config=None):
    config = config or SearchConfig()
    require_sha(current_sha256)
    if generation < 1 or config.games_per_generation != 128:
        raise ValueError("generation v1 requires 128 games")
    recent = list(dict.fromkeys(identity for identity in history if identity != current_sha256))[-4:]
    for identity in recent:
        require_sha(identity)
    rng = random.Random(f"fair-generation.v1:{seed}:{generation}")
    jobs = []
    used_seeds = set()
    for index in range(128):
        owner_models = [current_sha256, current_sha256]
        kind = "self"
        if generation >= 3 and index >= 96:
            if not recent:
                raise ValueError("history generation needs a published historical checkpoint")
            current_owner = index % 2
            owner_models[1 - current_owner] = recent[((index - 96) // 2) % len(recent)]
            kind = "history"
        match_seed = rng.randrange(2**32)
        while match_seed in used_seeds:
            match_seed = rng.randrange(2**32)
        used_seeds.add(match_seed)
        jobs.append(dict(episode_id=f"fair-g{generation:04d}-game{index:04d}", generation=generation,
                         current_sha256=current_sha256, owner_sha256=owner_models, kind=kind,
                         match_seed=match_seed, config_sha256=config.sha256, attempt=0))
    rng.shuffle(jobs)
    return jobs


def capacity_choice(samples):
    viable = [sample for sample in samples if sample["actors"] in (1, 2, 4)
              and sample["available_bytes"] >= 32 * 1024**3
              and sample["swap_in_bytes"] == 0 and sample["swap_out_bytes"] == 0
              and sample["failed_games"] == 0 and sample["completed_games"] > 0
              and sample["seconds"] > 0]
    if not viable:
        raise ValueError("no producer capacity passed memory and failure gates")
    return max(viable, key=lambda sample: (sample["completed_games"] / sample["seconds"], -sample["actors"]))["actors"]


class GenerationCoordinator:
    def __init__(self, directory, learner, *, champion_path, champion_sha256, history=(),
                 initial_optimizer_steps=None, schedule_seed=550001):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.learner = learner
        self.champion_path = Path(champion_path)
        self.champion_sha256 = require_sha(champion_sha256)
        self.history = list(history) or [self.champion_sha256]
        self.schedule_seed = schedule_seed
        self.initial_optimizer_steps = (learner.optimizer_steps if initial_optimizer_steps is None
                                        else initial_optimizer_steps)

    def _expected_jobs(self, generation):
        return {job["episode_id"]: job for job in generation_jobs(
            generation, self.champion_sha256, self.history, seed=self.schedule_seed,
            config=self.learner.config)}

    def _validate_episode(self, episode, expected):
        if episode.episode_id not in expected:
            raise ValueError("episode is not part of the frozen generation")
        job = expected[episode.episode_id]
        if (episode.generation != job["generation"]
                or episode.current_sha256 != self.champion_sha256
                or list(episode.owner_sha256) != job["owner_sha256"]):
            raise ValueError("generation episode does not match frozen job")
        if any(row.config_sha256 != self.learner.config.sha256 for row in episode.decisions):
            raise ValueError("generation search configuration differs")

    def ingest_episode(self, generation, episode):
        expected = self._expected_jobs(generation)
        self._validate_episode(episode, expected)
        return self.learner.ingest(episode)

    def save_stream_state(self, generation, path):
        return save_checkpoint(path, self.learner.model, self.learner.optimizer,
                               generation=generation, parent_sha256=self.champion_sha256,
                               config_sha256=self.learner.config.sha256,
                               replay_state=self.learner.replay.state_dict(directory=Path(path).parent),
                               sample_credit=self.learner.sample_credit,
                               optimizer_steps=self.learner.optimizer_steps,
                               replace=True)

    def _validate_generation(self, generation, manifest_paths, *, ingested=False):
        expected = self._expected_jobs(generation)
        seen = set()
        for path in manifest_paths:
            episode = load_episode(path)
            self._validate_episode(episode, expected)
            if episode.episode_id in seen:
                raise ValueError("generation contains duplicate episodes")
            seen.add(episode.episode_id)
            if ingested and self.learner.replay.identities.get(episode.episode_id) != semantic_digest(episode.to_payload()):
                raise ValueError("generation episode differs from ingested replay")
        if seen != set(expected):
            raise ValueError("generation barrier requires every distinct planned game")

    def finalize_generation(self, generation, manifest_paths, *, stream_state=None):
        self._validate_generation(generation, manifest_paths, ingested=True)
        if self.learner.optimizer_steps <= self.initial_optimizer_steps:
            raise ValueError("candidate needs an actual optimizer update in this generation")
        if self.learner.sample_credit >= self.learner.config.batch_size:
            raise ValueError("generation still has pending training credit")
        candidate_path = self.directory / f"candidate-g{generation:04d}.pt"
        if stream_state is not None:
            data = Path(stream_state).read_bytes()
            candidate_sha = hashlib.sha256(data).hexdigest()
            if candidate_path.exists():
                if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_sha:
                    raise ValueError("existing candidate differs from sealed stream state")
            else:
                atomic_write(candidate_path, data)
            return candidate_path, candidate_sha
        candidate_sha = save_checkpoint(candidate_path, self.learner.model, self.learner.optimizer,
                                        generation=generation, parent_sha256=self.champion_sha256,
                                        config_sha256=self.learner.config.sha256,
                                        replay_state=self.learner.replay.state_dict(),
                                        sample_credit=self.learner.sample_credit,
                                        optimizer_steps=self.learner.optimizer_steps)
        return candidate_path, candidate_sha

    def train_generation(self, generation, manifest_paths):
        manifest_paths = list(manifest_paths)
        self._validate_generation(generation, manifest_paths)
        for path in manifest_paths:
            self.ingest_episode(generation, load_episode(path))
        metrics = self.learner.train_available()
        if not metrics:
            raise ValueError("candidate needs an actual optimizer update")
        candidate_path, candidate_sha = self.finalize_generation(generation, manifest_paths)
        atomic_write(self.directory / f"training-g{generation:04d}.json",
                     json.dumps(dict(schema="fair-generation-training.v1", generation=generation,
                                     candidate_sha256=candidate_sha, metrics=metrics,
                                     config=asdict(self.learner.config)), allow_nan=False).encode())
        return candidate_path, candidate_sha

    def settle(self, generation, candidate_path, candidate_sha256, decision):
        candidate, payload, actual_sha = load_checkpoint(candidate_path,
                                                         device=next(self.learner.model.parameters()).device,
                                                         expected_sha256=candidate_sha256)
        if actual_sha != candidate_sha256 or payload["parent_sha256"] != self.champion_sha256 or payload["generation"] != generation:
            raise ValueError("candidate provenance differs at promotion")
        if decision.get("promote") is True:
            confirmation = decision.get("confirmation", {})
            if confirmation.get("games") != 2000 or confirmation.get("lower95", 0) <= .5:
                raise ValueError("promotion lacks independent confirmation")
            self.champion_path, self.champion_sha256 = Path(candidate_path), candidate_sha256
            self.history.append(candidate_sha256)
        else:
            incumbent, state, _ = load_checkpoint(self.champion_path,
                                                  device=next(self.learner.model.parameters()).device,
                                                  expected_sha256=self.champion_sha256)
            self.learner.model.load_state_dict(incumbent.state_dict())
            self.learner.optimizer.load_state_dict(state["optimizer"])
            self.learner.optimizer_steps = state["optimizer_steps"]
        publication = dict(schema="fair-search-publication.v1", generation=generation,
                           champion_path=str(self.champion_path), champion_sha256=self.champion_sha256,
                           history=self.history, evaluation=decision)
        atomic_write(self.directory / f"publication-g{generation:04d}.json",
                     json.dumps(publication, sort_keys=True, allow_nan=False).encode())
        return publication
