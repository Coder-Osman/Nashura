from collections import OrderedDict
import gzip
import hashlib
import io
from pathlib import Path
import random

import torch

from .contracts import SearchEpisode, require_sha, semantic_digest
from .storage import atomic_write, torch_bytes


def resolve_replay_state(state, directory):
    if state.get("schema") != "fair-search-replay.v2":
        return state
    resolved = dict(state, schema="fair-search-replay.v1", episodes=[])
    for reference in state["episodes"]:
        if set(reference) != {"episode_id", "sha256", "file"}:
            raise ValueError("replay reference fields differ")
        fingerprint = require_sha(state["identities"][reference["episode_id"]])
        if reference["file"] != f"replay-blobs/{fingerprint}.pt.gz":
            raise ValueError("replay reference path differs")
        data = (Path(directory) / reference["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != require_sha(reference["sha256"]):
            raise ValueError("replay blob checksum differs")
        raw = torch.load(io.BytesIO(gzip.decompress(data)), map_location="cpu", weights_only=True)
        if raw.get("episode_id") != reference["episode_id"]:
            raise ValueError("replay blob episode differs")
        resolved["episodes"].append(raw)
    return resolved


class ReplayPool:
    def __init__(self, *, max_generations=10, max_rows=1_000_000, seed=0):
        if max_generations < 1 or max_rows < 1:
            raise ValueError("invalid replay capacity")
        self.max_generations, self.max_rows = max_generations, max_rows
        self.episodes = OrderedDict()
        self.identities = {}
        self.highest_generation = 0
        self.rng = random.Random(seed)
        self._blob_references = {}

    @property
    def rows(self):
        return sum(len(episode.decisions) for episode in self.episodes.values())

    def add(self, episode):
        if not isinstance(episode, SearchEpisode):
            raise TypeError("replay accepts only completed search episodes")
        fingerprint = semantic_digest(episode.to_payload())
        if episode.episode_id in self.identities:
            if self.identities[episode.episode_id] != fingerprint:
                raise ValueError("duplicate episode has conflicting content")
            return 0
        if len(episode.decisions) > self.max_rows:
            raise ValueError("episode exceeds replay row capacity")
        if episode.generation < self.highest_generation - self.max_generations + 1:
            raise ValueError("episode is outside retained generations")
        self.highest_generation = max(self.highest_generation, episode.generation)
        self.identities[episode.episode_id] = fingerprint
        self.episodes[episode.episode_id] = episode
        minimum = self.highest_generation - self.max_generations + 1
        for identity, stored in list(self.episodes.items()):
            if stored.generation < minimum:
                del self.episodes[identity]
        while self.rows > self.max_rows:
            oldest = min(self.episodes, key=lambda identity: self.episodes[identity].generation)
            del self.episodes[oldest]
        return len(episode.decisions)

    def sample(self, count):
        if not self.episodes or count < 1:
            raise ValueError("replay is empty or sample size invalid")
        episodes = tuple(self.episodes.values())
        return [self.rng.choice(self.rng.choice(episodes).decisions) for _ in range(count)]

    def state_dict(self, *, directory=None):
        episodes = []
        for episode in self.episodes.values():
            if directory is None:
                episodes.append(episode.to_payload())
                continue
            fingerprint = self.identities[episode.episode_id]
            relative = f"replay-blobs/{fingerprint}.pt.gz"
            path = Path(directory) / relative
            key = (str(path.resolve()), fingerprint)
            if key not in self._blob_references:
                if path.exists():
                    data = path.read_bytes()
                    raw = torch.load(io.BytesIO(gzip.decompress(data)), map_location="cpu", weights_only=True)
                    if semantic_digest(raw) != fingerprint:
                        raise ValueError("existing replay blob identity differs")
                else:
                    raw = episode.to_payload()
                    if semantic_digest(raw) != fingerprint:
                        raise ValueError("replay episode changed after ingestion")
                    data = gzip.compress(torch_bytes(raw), compresslevel=1, mtime=0)
                    atomic_write(path, data)
                self._blob_references[key] = dict(episode_id=episode.episode_id, file=relative,
                                                   sha256=hashlib.sha256(data).hexdigest())
            episodes.append(dict(self._blob_references[key]))
        return dict(schema="fair-search-replay.v1", max_generations=self.max_generations,
                    max_rows=self.max_rows, highest_generation=self.highest_generation,
                    identities=dict(self.identities), rng=self.rng.getstate(),
                    episodes=episodes) | ({"schema": "fair-search-replay.v2"} if directory is not None else {})

    def load_state_dict(self, state):
        expected = {"schema", "max_generations", "max_rows", "highest_generation", "identities", "rng", "episodes"}
        if set(state) != expected or state["schema"] != "fair-search-replay.v1":
            raise ValueError("replay state contract differs")
        if (state["max_generations"], state["max_rows"]) != (self.max_generations, self.max_rows):
            raise ValueError("replay capacity differs on resume")
        restored = OrderedDict()
        for raw in state["episodes"]:
            episode = SearchEpisode.from_payload(raw)
            if episode.episode_id in restored or state["identities"].get(episode.episode_id) != semantic_digest(raw):
                raise ValueError("replay episode identity differs")
            restored[episode.episode_id] = episode
        if sum(len(episode.decisions) for episode in restored.values()) > self.max_rows:
            raise ValueError("restored replay exceeds capacity")
        self.episodes, self.identities = restored, dict(state["identities"])
        self.highest_generation = state["highest_generation"]
        self.rng.setstate(state["rng"])
        self._blob_references.clear()
