from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path


HOG_DECK = (26000021, 26000038, 26000014, 26000010, 26000030, 27000000, 28000000, 28000011)
PARENT_SHA256 = "e9f7c84a3e5f84a4d625c3f9a68e332a318a2aedc06e196da55bdb00a66bd1f0"


def load_search_config(path):
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("search configuration must be an object")
    return SearchConfig(**payload)


@dataclass(frozen=True)
class SearchConfig:
    schema: str = "fair-search-config.v1"
    simulations: int = 32
    particles: int = 8
    cpuct: float = 1.5
    candidates: int = 32
    initial_candidates: int = 8
    max_decisions: int = 6
    max_game_seconds: float = 10.0
    root_noise: float = 0.25
    noise_concentration: float = 10.0
    exploration_seconds: float = 60.0
    late_temperature: float = 0.5
    inference_batch: int = 32
    inference_wait_ms: float = 2.0
    inference_queue: int = 128
    learning_rate: float = 1e-4
    weight_decay: float = 1e-4
    gradient_clip: float = 1.0
    batch_size: int = 256
    replay_generations: int = 10
    replay_rows: int = 1_000_000
    samples_per_row: int = 2
    games_per_generation: int = 128
    generations: int = 10

    def __post_init__(self):
        if self.schema != "fair-search-config.v1" or self.simulations != 32:
            raise ValueError("FAIR v1 requires exactly 32 simulations")
        integers = (self.particles, self.candidates, self.initial_candidates, self.max_decisions,
                    self.inference_batch, self.inference_queue, self.batch_size,
                    self.replay_generations, self.replay_rows, self.samples_per_row,
                    self.games_per_generation, self.generations)
        if any(type(value) is not int or value < 1 for value in integers):
            raise ValueError("configuration integer bounds differ")
        if not 1 <= self.particles <= 8 or not self.initial_candidates <= self.candidates <= 32:
            raise ValueError("particle or candidate limit differs")
        positives = (self.cpuct, self.max_game_seconds, self.noise_concentration,
                     self.late_temperature, self.learning_rate, self.gradient_clip)
        if any(not math.isfinite(value) or value <= 0 for value in positives):
            raise ValueError("configuration positive scalar differs")
        if not 0 <= self.root_noise <= 1 or not 0 <= self.inference_wait_ms <= 20:
            raise ValueError("noise or inference wait bounds differ")
        if any(not math.isfinite(value) or value < 0 for value in
               (self.weight_decay, self.exploration_seconds)):
            raise ValueError("configuration nonnegative scalar differs")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()).hexdigest()
