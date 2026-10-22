import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

import torch

from .contracts import SearchEpisode, require_sha, semantic_digest


def atomic_write(path, data, *, replace=False):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".fair-", delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def torch_bytes(payload):
    stream = io.BytesIO()
    torch.save(payload, stream)
    return stream.getvalue()


def save_episode(directory, episode):
    if not isinstance(episode, SearchEpisode):
        raise TypeError("expected completed SearchEpisode")
    directory = Path(directory)
    if directory.exists():
        raise FileExistsError(directory)
    payload = episode.to_payload()
    recovery = dict(schema="fair-search-terminal-recovery.v1", episode=payload,
                    sha256=semantic_digest(payload))
    recovery_path = directory.with_name(directory.name + ".terminal.pt.gz")
    if recovery_path.exists():
        if semantic_digest(load_terminal_episode(recovery_path).to_payload()) != recovery["sha256"]:
            raise ValueError("terminal recovery identity differs")
    else:
        atomic_write(recovery_path, gzip.compress(torch_bytes(recovery), mtime=0))
    return _publish_episode(directory, episode)


def load_terminal_episode(path):
    raw = torch.load(io.BytesIO(gzip.decompress(Path(path).read_bytes())),
                     map_location="cpu", weights_only=True)
    if set(raw) != {"schema", "episode", "sha256"} or raw["schema"] != "fair-search-terminal-recovery.v1":
        raise ValueError("terminal recovery contract differs")
    require_sha(raw["sha256"])
    if semantic_digest(raw["episode"]) != raw["sha256"]:
        raise ValueError("terminal recovery checksum differs")
    return SearchEpisode.from_payload(raw["episode"])


def publish_terminal_episode(path, directory):
    return _publish_episode(Path(directory), load_terminal_episode(path), recover=True)


def _publish_episode(directory, episode, *, recover=False):
    directory.mkdir(parents=True, exist_ok=recover)
    data = gzip.compress(torch_bytes(episode.to_payload()), mtime=0)
    manifest = dict(schema="fair-search-episode-manifest.v2", episode_id=episode.episode_id,
                    generation=episode.generation, rows=len(episode.decisions), data_file="episode.pt.gz",
                    bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    files = {"episode.pt.gz": data, "completed.json": json.dumps(manifest, sort_keys=True).encode()}
    if recover:
        if any(path.name not in files or not path.is_file() for path in directory.iterdir()):
            raise ValueError("recovery destination contains unexpected files")
        for name, content in files.items():
            target = directory / name
            if target.exists() and target.read_bytes() != content:
                raise ValueError("recovery destination content differs")
    for name, content in files.items():
        target = directory / name
        if not recover or not target.exists():
            atomic_write(target, content)
    return directory / "completed.json"


def load_episode(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if set(manifest) != {"schema", "episode_id", "generation", "rows", "data_file", "bytes", "sha256"}:
        raise ValueError("episode manifest fields differ")
    if manifest["schema"] != "fair-search-episode-manifest.v2" or manifest["data_file"] != "episode.pt.gz":
        raise ValueError("episode manifest contract differs")
    require_sha(manifest["sha256"])
    data = (manifest_path.parent / manifest["data_file"]).read_bytes()
    if len(data) != manifest["bytes"] or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("episode container checksum differs")
    raw = torch.load(io.BytesIO(gzip.decompress(data)), map_location="cpu", weights_only=True)
    episode = SearchEpisode.from_payload(raw)
    if (episode.episode_id, episode.generation, len(episode.decisions)) != (
            manifest["episode_id"], manifest["generation"], manifest["rows"]):
        raise ValueError("manifest episode identity differs")
    return episode
