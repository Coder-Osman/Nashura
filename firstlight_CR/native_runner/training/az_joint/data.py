from dataclasses import dataclass, fields
import gzip
import hashlib
import io
import json
import re
from pathlib import Path

import torch

from ..fair_search.contracts import InformationState, pack, unpack, require_sha, semantic_digest
from ..fair_search.storage import atomic_write, torch_bytes
from .codec import JointActionCodec


def terminal_label(winner, owner):
    return 1 if winner is None else 0 if winner == owner else 2


def validate_terminal(terminal):
    expected = {"ended", "truncated", "winner", "terminal_tick", "reason", "rejected", "unconfirmed"}
    if (set(terminal) != expected or terminal["ended"] is not True or terminal["truncated"] is not False
            or terminal["rejected"] != 0 or terminal["unconfirmed"] != 0 or not terminal["reason"]
            or type(terminal["terminal_tick"]) is not int or terminal["terminal_tick"] < 1
            or (terminal["winner"] is not None and
                (type(terminal["winner"]) is not int or terminal["winner"] not in (0, 1)))):
        raise ValueError("joint data requires clean native complete terminal evidence")


@dataclass(frozen=True)
class JointDecision:
    index: int
    owner: int
    tick: int
    frame: object
    masks: dict
    selected: int
    model_sha256: str
    label: int
    teacher_action: int | None = None
    teacher_wdl: tuple | None = None
    visits: tuple | None = None

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0 or type(self.label) is not int or self.label not in (0, 1, 2):
            raise ValueError("invalid joint row identity or WDL class")
        require_sha(self.model_sha256)
        InformationState(self.owner, self.tick, self.frame, self.masks)
        legal = JointActionCodec.legal_mask(self.masks)[0]
        JointActionCodec.decode(self.selected)
        if not bool(legal[self.selected]):
            raise ValueError("illegal executed joint action")
        if self.teacher_action is not None:
            JointActionCodec.decode(self.teacher_action)
            if not bool(legal[self.teacher_action]) or self.visits is not None:
                raise ValueError("migration and search targets must be isolated")
            probabilities = torch.tensor(self.teacher_wdl, dtype=torch.float64)
            if (probabilities.shape != (3,) or not bool(torch.isfinite(probabilities).all())
                    or bool((probabilities < 0).any()) or abs(float(probabilities.sum()) - 1) > 1e-5):
                raise ValueError("invalid teacher WDL probabilities")
        elif self.teacher_wdl is not None:
            raise ValueError("orphan migration value target")
        if self.visits is not None:
            for action, count in self.visits:
                JointActionCodec.decode(action)
            if (not self.visits or len(self.visits) > 32 or len({item[0] for item in self.visits}) != len(self.visits)
                    or any(type(count) is not int or count <= 0 or not bool(legal[action])
                           for action, count in self.visits) or sum(count for _, count in self.visits) != 128
                    or dict(self.visits).get(self.selected, 0) < 1 or self.teacher_wdl is not None):
                raise ValueError("joint search requires exactly 128 legal visits")
            for action, _ in self.visits:
                JointActionCodec.decode(action)

    def to_payload(self):
        return {**vars(self), "frame": pack(self.frame), "masks": pack(self.masks)}

    @classmethod
    def from_payload(cls, raw):
        if set(raw) != {field.name for field in fields(cls)}:
            raise ValueError("joint row fields differ")
        return cls(**{**raw, "frame": unpack(raw["frame"]), "masks": unpack(raw["masks"])})


@dataclass(frozen=True)
class JointMigrationEpisode:
    episode_id: str
    seed: int
    split: str
    attempt: int
    generation: int
    config_sha256: str
    runtime_sha256: str
    source_sha256: str
    adapter_sha256: str
    decisions: tuple
    terminal: dict
    schema: str = "joint-migration-episode.v1"

    def __post_init__(self):
        validate_episode(self, migration=True)

    def to_payload(self):
        return {**vars(self), "decisions": [row.to_payload() for row in self.decisions]}


@dataclass(frozen=True)
class AZJointEpisode:
    episode_id: str
    seed: int
    split: str
    attempt: int
    generation: int
    config_sha256: str
    runtime_sha256: str
    owner_sha256: tuple
    decisions: tuple
    terminal: dict
    schema: str = "az-joint-episode.v1"

    def __post_init__(self):
        validate_episode(self, migration=False)

    def to_payload(self):
        return {**vars(self), "decisions": [row.to_payload() for row in self.decisions]}


def validate_episode(episode, *, migration):
    expected_schema = "joint-migration-episode.v1" if migration else "az-joint-episode.v1"
    if (episode.schema != expected_schema or not isinstance(episode.episode_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", episode.episode_id)
            or episode.split not in ("train", "validation", "test", "evaluation")
            or any(type(value) is not int or value < 0 for value in
                   (episode.seed, episode.attempt, episode.generation))):
        raise ValueError("invalid joint episode contract")
    for identity in (episode.config_sha256, episode.runtime_sha256):
        require_sha(identity)
    if migration:
        require_sha(episode.source_sha256)
        require_sha(episode.adapter_sha256)
    else:
        if len(episode.owner_sha256) != 2:
            raise ValueError("joint episode needs two model identities")
        for identity in episode.owner_sha256:
            require_sha(identity)
    validate_terminal(episode.terminal)
    if not episode.decisions or {row.owner for row in episode.decisions} != {0, 1}:
        raise ValueError("joint episode requires both players")
    previous = -1
    for index, row in enumerate(episode.decisions):
        if (not isinstance(row, JointDecision) or row.index != index
                or not previous <= row.tick <= episode.terminal["terminal_tick"]
                or row.label != terminal_label(episode.terminal["winner"], row.owner)):
            raise ValueError("joint chronology or player label differs")
        if migration != (row.teacher_action is not None) or migration == (row.visits is not None):
            raise ValueError("migration/search data boundary differs")
        if not migration and row.model_sha256 != episode.owner_sha256[row.owner]:
            raise ValueError("joint row model identity differs")
        previous = row.tick


def encode_episode(episode):
    if type(episode) not in (JointMigrationEpisode, AZJointEpisode):
        raise TypeError("expected independent joint episode")
    return gzip.compress(torch_bytes(episode.to_payload()), compresslevel=3, mtime=0)


def decode_episode(data, *, migration):
    raw = torch.load(io.BytesIO(gzip.decompress(data)), map_location="cpu", weights_only=True)
    episode_type = JointMigrationEpisode if migration else AZJointEpisode
    if not isinstance(raw, dict) or set(raw) != {field.name for field in fields(episode_type)}:
        raise ValueError("joint episode fields differ; old PPO/search/value data rejected")
    return episode_type(**{**raw, "decisions": tuple(JointDecision.from_payload(row) for row in raw["decisions"])})


def publish_episode(directory, data, *, migration, expected=None):
    episode = decode_episode(data, migration=migration)
    if expected is not None and any(getattr(episode, name) != value for name, value in expected.items()):
        raise ValueError("joint episode assignment differs")
    directory = Path(directory)
    identity = semantic_digest(episode.to_payload())
    marker = directory / "completed.json"
    if marker.exists():
        receipt = json.loads(marker.read_text())
        if receipt["semantic_sha256"] != identity:
            raise ValueError("conflicting duplicate joint episode")
        load_episode(marker, migration=migration)
        return receipt
    receipt = dict(schema=episode.schema + ".manifest", episode_id=episode.episode_id,
        split=episode.split, generation=episode.generation, rows=len(episode.decisions),
        sha256=hashlib.sha256(data).hexdigest(), semantic_sha256=identity, bytes=len(data), data_file="episode.pt.gz")
    atomic_write(directory / "episode.pt.gz", data, replace=True)
    atomic_write(marker, json.dumps(receipt, sort_keys=True).encode())
    return receipt


def load_episode(marker, *, migration):
    marker = Path(marker)
    receipt = json.loads(marker.read_text())
    schema = "joint-migration-episode.v1" if migration else "az-joint-episode.v1"
    if receipt.get("schema") != schema + ".manifest" or receipt.get("data_file") != "episode.pt.gz":
        raise ValueError("joint episode manifest differs")
    data = (marker.parent / "episode.pt.gz").read_bytes()
    if len(data) != receipt["bytes"] or hashlib.sha256(data).hexdigest() != receipt["sha256"]:
        raise ValueError("joint episode checksum differs")
    episode = decode_episode(data, migration=migration)
    if (semantic_digest(episode.to_payload()) != receipt["semantic_sha256"]
            or episode.episode_id != receipt["episode_id"] or episode.split != receipt["split"]
            or episode.generation != receipt["generation"] or len(episode.decisions) != receipt["rows"]):
        raise ValueError("joint episode manifest content differs")
    return episode
