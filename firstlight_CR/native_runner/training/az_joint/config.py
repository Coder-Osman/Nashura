from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class JointConfig:
    schema: str = "az-joint-config.v1"
    seed: int = 20261019
    simulations: int = 128
    particles: int = 1
    cpuct: float = 1.5
    candidates: int = 32
    initial_candidates: int = 8
    max_decisions: int = 6
    max_game_seconds: float = 10.
    root_noise: float = .25
    noise_concentration: float = 10.
    inference_batch: int = 32
    inference_wait_ms: float = 2.
    inference_queue: int = 128
    learning_rate: float = 1e-4
    weight_decay: float = 1e-4
    gradient_clip: float = 1.
    batch_size: int = 256
    micro_batch: int = 8
    replay_generations: int = 10
    replay_rows: int = 1_000_000
    samples_per_row: int = 2
    games_per_generation: int = 512
    validation_games: int = 32
    validation_rows: int = 64
    validation_generations: int = 5
    validation_interval: int = 128
    validation_improvement: float = 1e-6
    cold_start_patience: int = 5

    def __post_init__(self):
        if (self.schema, self.simulations, self.particles, self.candidates, self.initial_candidates) != (
                "az-joint-config.v1", 128, 1, 32, 8):
            raise ValueError("joint action/search version contract differs")
        integer_names = ("seed", "inference_batch", "inference_queue", "batch_size", "micro_batch",
                         "replay_generations", "replay_rows", "samples_per_row", "games_per_generation",
                         "validation_games", "validation_rows", "validation_generations", "validation_interval",
                         "cold_start_patience", "max_decisions")
        if any(type(getattr(self, name)) is not int or getattr(self, name) <= 0 for name in integer_names):
            raise ValueError("invalid joint integer configuration")
        if self.batch_size % self.micro_batch or self.validation_games >= self.games_per_generation:
            raise ValueError("invalid batch or dataset split")
        for name in ("cpuct", "max_game_seconds", "noise_concentration", "learning_rate", "gradient_clip"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError("invalid joint positive configuration")
        if not 0 <= self.root_noise <= 1 or not 0 <= self.inference_wait_ms <= 20:
            raise ValueError("invalid joint exploration or batching")
        if any(not math.isfinite(value) or value < 0 for value in (self.weight_decay, self.validation_improvement)):
            raise ValueError("invalid joint training configuration")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()).hexdigest()

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text()))


def stream_seed(namespace, *identities):
    data = json.dumps(["az-joint-rng.v1", namespace, *identities], separators=(",", ":"), allow_nan=False)
    return int.from_bytes(hashlib.sha256(data.encode()).digest()[:8], "big")
