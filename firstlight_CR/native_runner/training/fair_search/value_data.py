from dataclasses import dataclass, fields
import gzip
import hashlib
import io
import json
from pathlib import Path
import random

import torch

from .contracts import InformationState, pack, unpack, require_sha, semantic_digest
from .storage import atomic_write, torch_bytes


BASE_SHA = "ddfb5c3a7cf0be236093c926e9c3ddb23731c150bd018db5cc669d24250ac71f"
SAMPLING = dict(mode="policy-sample", temperatures=[1., 1., 1., 1.], search=False)
SAMPLING_SHA = semantic_digest(SAMPLING)


def make_manifest(runtime_sha256):
    require_sha(runtime_sha256)
    indices = list(range(4096))
    random.Random(20261012).shuffle(indices)
    splits = {index: "train" if rank < 3072 else "validation" if rank < 3584 else "test"
              for rank, index in enumerate(indices)}
    return dict(schema="fair-value-run.v1", checkpoint_sha256=BASE_SHA,
                runtime_sha256=runtime_sha256, sampling=SAMPLING,
                jobs=[dict(episode_id=f"value-{index:05d}", seed=12610000 + index,
                           split=splits[index]) for index in range(4096)])


def terminal_label(winner, owner):
    return 1 if winner is None else 0 if winner == owner else 2


@dataclass(frozen=True)
class ValueDecision:
    index: int
    owner: int
    tick: int
    frame: object
    masks: dict
    selected: str
    label: int

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0 or type(self.owner) is not int:
            raise ValueError("invalid value decision identity")
        if type(self.label) is not int or self.label not in (0, 1, 2):
            raise ValueError("invalid WDL label")
        information = InformationState(self.owner, self.tick, self.frame, self.masks)
        if not information.legal(self.selected):
            raise ValueError("illegal direct action")

    def to_payload(self):
        return {**vars(self), "frame": pack(self.frame), "masks": pack(self.masks)}

    @classmethod
    def from_payload(cls, payload):
        if set(payload) != {field.name for field in fields(cls)}:
            raise ValueError("value decision fields differ")
        return cls(**{**payload, "frame": unpack(payload["frame"]), "masks": unpack(payload["masks"])})


@dataclass(frozen=True)
class ValueEpisode:
    episode_id: str
    seed: int
    split: str
    attempt: int
    model_sha256: str
    runtime_sha256: str
    sampling_sha256: str
    decisions: tuple
    terminal: dict
    schema: str = "fair-value-episode.v1"

    def __post_init__(self):
        if self.schema != "fair-value-episode.v1" or not self.episode_id:
            raise ValueError("expected independent value episode")
        if self.split not in ("train", "validation", "test"):
            raise ValueError("invalid split")
        if type(self.seed) is not int or type(self.attempt) is not int or self.attempt < 0:
            raise ValueError("invalid seed or attempt")
        for identity in (self.model_sha256, self.runtime_sha256, self.sampling_sha256):
            require_sha(identity)
        if self.sampling_sha256 != SAMPLING_SHA:
            raise ValueError("value data requires full policy sampling")
        terminal = self.terminal
        if set(terminal) != {"ended", "truncated", "winner", "terminal_tick", "reason", "rejected", "unconfirmed"}:
            raise ValueError("terminal evidence fields differ")
        if (terminal["ended"] is not True or terminal["truncated"] is not False
                or terminal["rejected"] != 0 or terminal["unconfirmed"] != 0
                or not terminal["reason"] or type(terminal["terminal_tick"]) is not int
                or terminal["terminal_tick"] < 1
                or (terminal["winner"] is not None and
                    (type(terminal["winner"]) is not int or terminal["winner"] not in (0, 1)))):
            raise ValueError("value episode requires clean native complete terminal")
        if not self.decisions or {row.owner for row in self.decisions} != {0, 1}:
            raise ValueError("value episode requires both players")
        previous_tick = -1
        for index, row in enumerate(self.decisions):
            if not isinstance(row, ValueDecision) or row.index != index:
                raise ValueError("value decisions are not contiguous")
            if not previous_tick <= row.tick <= terminal["terminal_tick"]:
                raise ValueError("value decision time differs")
            previous_tick = row.tick
            if row.label != terminal_label(terminal["winner"], row.owner):
                raise ValueError("value label perspective differs")

    def to_payload(self):
        return {**vars(self), "decisions": [row.to_payload() for row in self.decisions]}

    @classmethod
    def from_payload(cls, payload):
        if set(payload) != {field.name for field in fields(cls)}:
            raise ValueError("value episode fields differ; search/PPO data rejected")
        return cls(**{**payload, "decisions": tuple(ValueDecision.from_payload(row)
                                                   for row in payload["decisions"])})


def encode_episode(episode):
    if not isinstance(episode, ValueEpisode):
        raise TypeError("expected ValueEpisode")
    return gzip.compress(torch_bytes(episode.to_payload()), compresslevel=3, mtime=0)


def decode_episode(data):
    return ValueEpisode.from_payload(torch.load(io.BytesIO(gzip.decompress(data)),
                                               map_location="cpu", weights_only=True))


def publish_episode(directory, data, *, expected_job=None, model_sha256=None, runtime_sha256=None):
    episode = decode_episode(data)
    if expected_job is not None and any(getattr(episode, key) != expected_job[key]
                                        for key in ("episode_id", "seed", "split", "attempt")):
        raise ValueError("episode assignment differs")
    if model_sha256 is not None and episode.model_sha256 != model_sha256:
        raise ValueError("producing model differs")
    if runtime_sha256 is not None and episode.runtime_sha256 != runtime_sha256:
        raise ValueError("engine identity differs")
    directory = Path(directory)
    identity = semantic_digest(episode.to_payload())
    marker = directory / "completed.json"
    if marker.exists():
        existing = json.loads(marker.read_text())
        if existing["semantic_sha256"] != identity:
            raise ValueError("conflicting completed episode")
        load_episode(marker)
        return existing
    manifest = dict(schema="fair-value-manifest.v1", episode_id=episode.episode_id,
                    split=episode.split, rows=len(episode.decisions), bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(), semantic_sha256=identity,
                    data_file="episode.pt.gz")
    atomic_write(directory / "episode.pt.gz", data, replace=True)
    atomic_write(marker, json.dumps(manifest, sort_keys=True).encode())
    return manifest


def load_episode(marker):
    marker = Path(marker)
    manifest = json.loads(marker.read_text())
    if manifest.get("schema") != "fair-value-manifest.v1" or manifest.get("data_file") != "episode.pt.gz":
        raise ValueError("expected independent value manifest")
    data = (marker.parent / manifest["data_file"]).read_bytes()
    if len(data) != manifest["bytes"] or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("value episode checksum differs")
    episode = decode_episode(data)
    if (semantic_digest(episode.to_payload()) != manifest["semantic_sha256"]
            or episode.episode_id != manifest["episode_id"] or episode.split != manifest["split"]
            or len(episode.decisions) != manifest["rows"]):
        raise ValueError("value episode content identity differs")
    return episode
