import gzip
import hashlib
import io
import json
import os
from pathlib import Path

import torch

from ..fair_search.contracts import require_sha, semantic_digest
from ..fair_search.storage import atomic_write, torch_bytes
from .contracts import AZJointEpisode, IMPLEMENTATION, exact_fields, validate_identity
from .codec import ActionCodec


def durable_write(path, data):
    path = Path(path)
    atomic_write(path, data)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_immutable(path, data):
    try:
        durable_write(path, data)
    except FileExistsError:
        if Path(path).read_bytes() != data:
            raise ValueError("immutable artifact identity conflict")


def json_bytes(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def validate_dataset(manifest):
    exact_fields(manifest, {"schema", "implementation", "namespace_sha", "identity", "card_ids", "matches"}, "dataset manifest")
    if manifest["schema"] != "FairFinalDataset/v1" or manifest["implementation"] != IMPLEMENTATION:
        raise ValueError("independent frozen dataset required")
    require_sha(manifest["namespace_sha"])
    validate_identity(manifest["identity"])
    ActionCodec(manifest["card_ids"])
    if not isinstance(manifest["matches"], dict) or not manifest["matches"]:
        raise ValueError("pre-registered match splits required")
    pairs = {}
    for match_id, task in manifest["matches"].items():
        require_sha(match_id)
        exact_fields(task, {"pair_seed_id", "split"}, "registered match")
        require_sha(task["pair_seed_id"])
        if task["split"] not in ("train", "validation", "test", "confirmation"):
            raise ValueError("unknown dataset split")
        if task["pair_seed_id"] in pairs and pairs[task["pair_seed_id"]] != task["split"]:
            raise ValueError("paired games cannot leak across splits")
        pairs[task["pair_seed_id"]] = task["split"]
    return semantic_digest(manifest)


class EpisodeStore:
    def __init__(self, root, *, manifest):
        self.root = Path(root)
        self.manifest = json.loads(json_bytes(manifest))
        self.dataset_sha = validate_dataset(self.manifest)
        ensure_immutable(self.root / "dataset.json", json_bytes(self.manifest))

    def _admit(self, episode):
        if not isinstance(episode, AZJointEpisode):
            raise TypeError("only independent AZJointEpisode/v1 is admitted")
        episode.validate()
        payload = episode.payload
        task = self.manifest["matches"].get(episode.match_id)
        if (task is None or payload["dataset_manifest_sha"] != self.dataset_sha
                or payload["identity"] != self.manifest["identity"]
                or payload["card_ids"] != self.manifest["card_ids"]
                or {key: payload[key] for key in ("pair_seed_id", "split")} != task):
            raise ValueError("episode differs from pre-registered dataset or split")

    def stage(self, episode):
        self._admit(episode)
        payload = episode.to_payload()
        digest = semantic_digest(payload)
        envelope = dict(schema="FairFinalTerminalRecovery/v1", sha256=digest, episode=payload)
        data = gzip.compress(torch_bytes(envelope), compresslevel=1, mtime=0)
        path = self.root / "staged" / f"{digest}.pt.gz"
        try:
            durable_write(path, data)
        except FileExistsError:
            existing, _ = self._read_staged(path)
            if existing.sha256 != digest:
                raise ValueError("staged episode identity conflict")
        return digest

    def _read_staged(self, path):
        data = path.read_bytes()
        envelope = torch.load(io.BytesIO(gzip.decompress(data)), weights_only=True, map_location="cpu")
        exact_fields(envelope, {"schema", "sha256", "episode"}, "terminal recovery")
        if envelope["schema"] != "FairFinalTerminalRecovery/v1":
            raise ValueError("terminal recovery schema differs")
        require_sha(envelope["sha256"])
        episode = AZJointEpisode(envelope["episode"])
        self._admit(episode)
        if episode.sha256 != envelope["sha256"] or path.name != episode.sha256 + ".pt.gz":
            raise ValueError("terminal recovery semantic checksum differs")
        return episode, data

    def publish(self, staged_sha):
        require_sha(staged_sha)
        episode, data = self._read_staged(self.root / "staged" / f"{staged_sha}.pt.gz")
        payload = episode.payload
        receipt = dict(schema="FairFinalEpisodeReceipt/v1", match_id=episode.match_id,
            dataset_sha=self.dataset_sha, semantic_sha=staged_sha, sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data), attempt_id=payload["attempt_id"], rows=len(payload["decisions"]), split=payload["split"])
        marker = self.root / "completed" / f"{episode.match_id}.json"
        try:
            durable_write(marker, json_bytes(receipt))
        except FileExistsError:
            previous, _ = self.load(episode.match_id)
            if previous.payload["identity"] != payload["identity"]:
                raise ValueError("successful match identity conflict")
            return dict(status="duplicate", match_id=episode.match_id, semantic_sha=previous.sha256)
        self.load(episode.match_id)
        return dict(status="published", match_id=episode.match_id, semantic_sha=staged_sha)

    def load(self, match_id):
        require_sha(match_id)
        receipt = json.loads((self.root / "completed" / f"{match_id}.json").read_text())
        exact_fields(receipt, {"schema", "match_id", "dataset_sha", "semantic_sha", "sha256",
            "bytes", "attempt_id", "rows", "split"}, "episode receipt")
        require_sha(receipt["semantic_sha"])
        if (receipt["schema"] != "FairFinalEpisodeReceipt/v1" or receipt["match_id"] != match_id
                or receipt["dataset_sha"] != self.dataset_sha):
            raise ValueError("episode receipt dataset differs")
        episode, data = self._read_staged(self.root / "staged" / f"{receipt['semantic_sha']}.pt.gz")
        if (hashlib.sha256(data).hexdigest() != receipt["sha256"] or len(data) != receipt["bytes"]
                or episode.match_id != match_id or episode.payload["attempt_id"] != receipt["attempt_id"]
                or len(episode.payload["decisions"]) != receipt["rows"] or episode.payload["split"] != receipt["split"]):
            raise ValueError("episode receipt content checksum differs")
        return episode, receipt
