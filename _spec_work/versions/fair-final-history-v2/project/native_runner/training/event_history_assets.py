"""Batch history verification without importing Torch on a cache hit.

Descriptors cache export metadata, never proof of file integrity. Every round
still hashes the complete archive checkpoints and every host's artifacts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time

VERSION = "history-preparation.v2"
RESERVE_BYTES = 1_500_000_000


def sha256_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular historical weight: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid historical SHA")
    return value


def validate_descriptor(descriptor, source_sha, contract_sha):
    valid_sha(source_sha)
    valid_sha(contract_sha)
    valid_sha(descriptor["artifact_sha256"])
    if descriptor["source_checkpoint_sha256"] != source_sha or descriptor["contract_sha256"] != contract_sha:
        raise ValueError("historical source identity or contract mismatch")
    if type(descriptor["bytes"]) is not int or descriptor["bytes"] <= 0:
        raise ValueError("invalid historical artifact size")


def artifact_path(root, descriptor):
    return Path(root) / "history-inference" / (valid_sha(descriptor["artifact_sha256"]) + ".pt")


def verify_artifact(root, descriptor):
    path = artifact_path(root, descriptor)
    if path.stat().st_size != descriptor["bytes"] or sha256_file(path) != descriptor["artifact_sha256"]:
        raise ValueError("historical inference artifact SHA or size mismatch")
    return path


def archive_descriptors(root, sources, contract_sha):
    """One archive request for the whole pool; export only missing indices."""
    started = time.perf_counter()
    root = Path(root)
    valid_sha(contract_sha)
    if not sources or len(set(sources)) != len(sources):
        raise ValueError("empty or duplicate historical sources")
    directory = root / "history-inference"
    directory.mkdir(exist_ok=True)
    descriptors = {}
    exported = 0
    for sha in sources:
        source = root / "weights" / (valid_sha(sha) + ".pt")
        if sha256_file(source) != sha:
            raise ValueError("archive checkpoint SHA mismatch")
        index = directory / (sha + ".json")
        if index.exists():
            if index.is_symlink():
                raise ValueError("historical index must not be a symlink")
            descriptor = json.loads(index.read_text())
        else:
            if shutil.disk_usage(root).free <= RESERVE_BYTES + source.stat().st_size:
                raise RuntimeError("historical export disk guard")
            # A hit must not import Torch, initialize CUDA, or re-export tensors.
            from .event_inference import export_inference
            descriptor = export_inference(source, directory)
            exported += 1
        validate_descriptor(descriptor, sha, contract_sha)
        expected_path = artifact_path(root, descriptor)
        if Path(descriptor["path"]) != expected_path:
            raise ValueError("historical descriptor path mismatch")
        verify_artifact(root, descriptor)
        if not index.exists():
            temporary = index.with_suffix(".history-new")
            temporary.write_text(json.dumps(descriptor))
            temporary.replace(index)
        descriptors[sha] = descriptor
    return dict(descriptors=descriptors, exported=exported, seconds=time.perf_counter()-started)


def probe_artifacts(root, descriptors, contract_sha, *, current_bytes=0, require_complete=False):
    """Hash all present files; corruption fails, absence alone allows transfer."""
    started = time.perf_counter()
    root = Path(root)
    valid_sha(contract_sha)
    if type(current_bytes) is not int or current_bytes < 0:
        raise ValueError("invalid current artifact size")
    missing = []
    for sha, descriptor in descriptors.items():
        validate_descriptor(descriptor, sha, contract_sha)
        path = artifact_path(root, descriptor)
        if path.is_symlink():
            raise ValueError("historical artifact must not be a symlink")
        if path.exists():
            verify_artifact(root, descriptor)
        else:
            missing.append(sha)
    required = RESERVE_BYTES + current_bytes + sum(descriptors[sha]["bytes"] for sha in missing)
    if shutil.disk_usage(root).free <= required:
        raise RuntimeError("historical model disk guard")
    if missing and require_complete:
        raise ValueError("historical model missing after transfer")
    (root / "history-inference").mkdir(exist_ok=True)
    return dict(missing=missing, verified=len(descriptors)-len(missing),
                seconds=time.perf_counter()-started)


def main():
    import sys
    task = json.loads(sys.argv[1])
    operation = task.pop("operation")
    if operation == "archive":
        result = archive_descriptors(**task)
    elif operation == "probe":
        result = probe_artifacts(**task)
    else:
        raise ValueError("unknown history operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
