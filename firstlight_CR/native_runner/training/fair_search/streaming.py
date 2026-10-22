import hashlib
import json
import os
from pathlib import Path
import time

from .coordinator import GenerationCoordinator
from .learner import SearchLearner
from .model import load_checkpoint
from .replay import ReplayPool
from .storage import atomic_write, load_episode


def publish_status(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True, allow_nan=False).encode(), replace=True)


class StreamingGeneration:
    def __init__(self, root, manifest, config, *, device="cuda:0", checkpoint_steps=8):
        self.root = Path(root)
        self.manifest = manifest
        self.config = config
        self.jobs = {job["episode_id"]: job for job in manifest["jobs"]}
        generations = {job["generation"] for job in self.jobs.values()}
        if len(generations) != 1 or len(self.jobs) != 128 or len(manifest["jobs"]) != 128:
            raise ValueError("stream requires 128 distinct jobs from one generation")
        if type(checkpoint_steps) is not int or checkpoint_steps < 1:
            raise ValueError("checkpoint interval must be positive")
        self.generation = generations.pop()
        self.checkpoint_steps = checkpoint_steps
        self.training = self.root / "training"
        self.training.mkdir(parents=True, exist_ok=True)
        self.checkpoint = self.training / f"stream-g{self.generation:04d}.pt"
        self.session_path = self.training / f"stream-g{self.generation:04d}.json"
        self.ready_path = self.root / "candidate-ready.json"
        self.status_path = self.root / "learner-state.json"
        self.started = time.time()
        contract = dict(schema="fair-stream-session.v1", generation=self.generation,
                        champion_sha256=manifest["checkpoint_sha256"], config_sha256=config.sha256,
                        jobs_sha256=hashlib.sha256(json.dumps(manifest["jobs"], sort_keys=True).encode()).hexdigest(),
                        history=manifest.get("history", []))
        if "schedule_seed" in manifest:
            contract["schedule_seed"] = manifest["schedule_seed"]
        if manifest["config_sha256"] != config.sha256:
            raise ValueError("manifest configuration identity differs")
        if self.session_path.exists():
            self.session = json.loads(self.session_path.read_text())
            if ({key: self.session.get(key) for key in contract} != contract
                    or type(self.session.get("initial_optimizer_steps")) is not int):
                raise ValueError("stream session differs from frozen generation")
        else:
            if self.checkpoint.exists():
                raise ValueError("stream checkpoint lacks session provenance")
            self.session = None
        source = self.checkpoint if self.checkpoint.exists() else self.root / "initial.pt"
        model, payload, identity = load_checkpoint(
            source, device=device, resume=True,
            expected_sha256=None if source == self.checkpoint else manifest["checkpoint_sha256"])
        if source == self.checkpoint and (payload["generation"] != self.generation
                                         or payload["parent_sha256"] != manifest["checkpoint_sha256"]):
            raise ValueError("stream checkpoint generation or parent differs")
        if self.session is None:
            self.session = dict(contract, initial_optimizer_steps=payload["optimizer_steps"])
            atomic_write(self.session_path, json.dumps(self.session, sort_keys=True).encode())
        self.learner = SearchLearner(model, ReplayPool(max_generations=config.replay_generations,
                                                      max_rows=config.replay_rows), config=config)
        self.learner.resume(payload)
        self.coordinator = GenerationCoordinator(
            self.training, self.learner, champion_path=self.root / "initial.pt",
            champion_sha256=manifest["checkpoint_sha256"], history=manifest.get("history", ()),
            schedule_seed=manifest.get("schedule_seed", 550001),
            initial_optimizer_steps=self.session["initial_optimizer_steps"])
        expected = self.coordinator._expected_jobs(self.generation)
        if set(expected) != set(self.jobs):
            raise ValueError("manifest jobs differ from frozen generation")
        for episode_id, job in self.jobs.items():
            if any(job.get(key) != expected[episode_id][key] for key in
                   ("generation", "current_sha256", "owner_sha256", "config_sha256", "match_seed", "kind")):
                raise ValueError("manifest job differs from frozen schedule")
        self.seen = set(self.learner.replay.identities) & set(self.jobs)
        for episode in self.learner.replay.episodes.values():
            if episode.generation == self.generation:
                self.coordinator._validate_episode(episode, expected)
        self.durable_steps = self.learner.optimizer_steps
        self.checkpoint_sha256 = identity if source == self.checkpoint else None
        self.last_metrics = None
        self.timings = {}
        self._status("resuming" if source == self.checkpoint else "waiting-for-episodes")

    def _status(self, stage, **details):
        publish_status(self.status_path, dict(
            stage=stage, pid=os.getpid(), time=time.time(), generation=self.generation,
            champion_sha256=self.manifest["checkpoint_sha256"], ingested=len(self.seen),
            required=len(self.jobs), replay_rows=self.learner.replay.rows,
            optimizer_steps=self.learner.optimizer_steps, durable_steps=self.durable_steps,
            sample_credit=self.learner.sample_credit, checkpoint_sha256=self.checkpoint_sha256,
            latest_metrics=self.last_metrics, timings=self.timings, **details))

    def save(self):
        self._status("checkpointing")
        started = time.perf_counter()
        self.checkpoint_sha256 = self.coordinator.save_stream_state(self.generation, self.checkpoint)
        self.durable_steps = self.learner.optimizer_steps
        self.timings["checkpoint_seconds"] = time.perf_counter() - started

    def _ingest_available(self, *, limit=1):
        if type(limit) is not int or limit < 1:
            raise ValueError("ingest limit must be a positive integer")
        added = 0
        accepted = 0
        for episode_id in self.jobs:
            if episode_id in self.seen:
                continue
            directory = self.root / "replay" / episode_id
            validated = directory / "validated.json"
            if not validated.exists():
                continue
            self._status("ingesting", current_episode=episode_id)
            record = json.loads(validated.read_text())
            episode = load_episode(directory / "completed.json")
            if (episode.episode_id != episode_id or record.get("episode_id") != episode_id
                    or record.get("rows") != len(episode.decisions)
                    or record.get("attempt") != episode.attempt
                    or any(row.runtime_sha256 != record.get("runtime_sha256") for row in episode.decisions)):
                raise ValueError("validated episode identity differs")
            added += self.coordinator.ingest_episode(self.generation, episode)
            self.seen.add(episode_id)
            accepted += 1
            if accepted >= limit:
                break
        return added

    def _ready(self):
        if not self.ready_path.exists():
            return False
        ready = json.loads(self.ready_path.read_text())
        candidate = self.training / f"candidate-g{self.generation:04d}.pt"
        if (ready.get("stage") != "awaiting-independent-evaluation"
                or ready.get("generation") != self.generation
                or ready.get("parent_sha256") != self.manifest["checkpoint_sha256"]
                or ready.get("promoted") is not False
                or Path(ready.get("candidate", "")) != candidate
                or ready.get("sha256") != self.checkpoint_sha256
                or self.seen != set(self.jobs)
                or self.learner.sample_credit >= self.config.batch_size
                or self.learner.optimizer_steps <= self.session["initial_optimizer_steps"]
                or hashlib.sha256(candidate.read_bytes()).hexdigest() != ready["sha256"]):
            raise ValueError("candidate readiness differs from stream checkpoint")
        self._status("awaiting-independent-evaluation")
        return True

    def tick(self):
        if self._ready():
            return True
        started = time.perf_counter()
        added = self._ingest_available()
        self.timings["ingest_seconds"] = time.perf_counter() - started
        if added:
            self.save()
        self._status("training" if self.learner.sample_credit >= self.config.batch_size else "waiting-for-episodes")
        metrics = self.learner.train_available(max_steps=1)
        if metrics:
            self.last_metrics = metrics[-1]
            print(json.dumps(dict(generation=self.generation, ingested=len(self.seen), **self.last_metrics)), flush=True)
        complete = self.seen == set(self.jobs) and self.learner.sample_credit < self.config.batch_size
        if self.learner.optimizer_steps > self.durable_steps and (
                self.learner.optimizer_steps - self.durable_steps >= self.checkpoint_steps
                or self.learner.sample_credit < self.config.batch_size or complete):
            self.save()
        if complete:
            self._status("sealing-candidate")
            paths = [self.root / "replay" / episode_id / "completed.json" for episode_id in self.jobs]
            candidate, identity = self.coordinator.finalize_generation(
                self.generation, paths, stream_state=self.checkpoint)
            ready = dict(candidate=str(candidate), sha256=identity, generation=self.generation,
                         parent_sha256=self.manifest["checkpoint_sha256"], promoted=False,
                         optimizer_steps=self.learner.optimizer_steps, sample_credit=self.learner.sample_credit,
                         seconds=time.time() - self.started, stage="awaiting-independent-evaluation")
            publish_status(self.ready_path, ready)
            self._status("awaiting-independent-evaluation")
            return True
        self._status("training" if self.learner.sample_credit >= self.config.batch_size else "waiting-for-episodes")
        return False

    def run(self, *, poll_seconds=5):
        try:
            while not self.tick():
                if self.learner.sample_credit < self.config.batch_size:
                    time.sleep(poll_seconds)
        except Exception as error:
            self._status("failed", error=str(error))
            raise
