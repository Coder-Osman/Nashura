"""Verified inference artifacts bound to their complete training checkpoint."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import torch

from .event_checkpoint import (
    CONTRACT_KEY, INITIALIZATION_KEY, _build_model, _contract, _contract_hash, _resolve,
)

VERSION = "event-inference.v1"


def _validate(payload, *, initialization_only=False):
    if payload.get(CONTRACT_KEY, {}).get("version") not in {"event-checkpoint.v2", "event-checkpoint.v3", "event-checkpoint.v4", "event-checkpoint.v5"}:
        raise ValueError("inference requires an explicit current checkpoint contract")
    configs = _resolve(payload, None, None)
    contract_hash = _contract_hash(_contract(*configs))
    if payload.get("contract_sha256") != contract_hash:
        raise ValueError("inference contract hash mismatch")
    if not configs[0].snapshot_only:
        raise ValueError("persistent event collector requires snapshot_only")
    if payload.get(INITIALIZATION_KEY, {}).get("needs_recalibration_or_finetune"):
        if not (initialization_only and configs[1].ability_actions):
            raise ValueError("inference checkpoint still needs recalibration")
    if not payload.get("model") or any(not torch.isfinite(t).all() for t in payload["model"].values()):
        raise ValueError("inference model is empty or nonfinite")
    return configs, contract_hash


def export_inference(source: Path, directory: Path, *, initialization_only=False) -> dict:
    data = source.read_bytes()
    source_sha = hashlib.sha256(data).hexdigest()
    payload = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    _, contract_hash = _validate(payload, initialization_only=initialization_only)
    artifact = {key: payload[key] for key in ("model", CONTRACT_KEY, "contract_sha256", INITIALIZATION_KEY)
                if key in payload}
    artifact.update(inference_version=VERSION, source_checkpoint_sha256=source_sha,
                    policy_version=str(payload.get("policy_version") or source_sha))
    if initialization_only:
        artifact["initialization_only"] = True
    buffer = io.BytesIO()
    torch.save(artifact, buffer)
    content = buffer.getvalue()
    artifact_sha = hashlib.sha256(content).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (artifact_sha + ".pt")
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact_sha:
            raise ValueError("existing inference artifact is corrupt")
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
    descriptor = dict(path=str(path), artifact_sha256=artifact_sha, source_checkpoint_sha256=source_sha,
                      contract_sha256=contract_hash, bytes=len(content), source_bytes=len(data))
    if initialization_only:
        descriptor["initialization_only"] = True
    return descriptor


class InferenceModel:
    """Only one live model per worker; contracts cannot change during a run."""

    def __init__(self):
        self.model = None
        self.contract_sha256 = None
        self.artifact_sha256 = None

    def load(self, descriptor):
        data = Path(descriptor["path"]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != descriptor["artifact_sha256"]:
            raise ValueError("inference artifact SHA mismatch")
        payload = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
        if payload.get("inference_version") != VERSION or "event_ppo_training" in payload:
            raise ValueError("not a pure inference artifact")
        if payload.get("source_checkpoint_sha256") != descriptor["source_checkpoint_sha256"]:
            raise ValueError("source checkpoint SHA mismatch")
        initialization_only = descriptor.get("initialization_only", False)
        if type(initialization_only) is not bool or payload.get("initialization_only", False) != initialization_only:
            raise ValueError("inference initialization purpose mismatch")
        configs, contract_hash = _validate(payload, initialization_only=initialization_only)
        if contract_hash != descriptor["contract_sha256"]:
            raise ValueError("task contract SHA mismatch")
        if self.model is not None and self.contract_sha256 != contract_hash:
            raise ValueError("worker contract changed; restart with a new run")
        if self.model is None:
            self.model = _build_model(*configs)
        self.model.load_state_dict(payload["model"], strict=True)
        self.model.eval()
        self.contract_sha256 = contract_hash
        self.artifact_sha256 = digest
        return self.model, payload["policy_version"]


if __name__ == "__main__":
    import sys
    print(json.dumps(export_inference(Path(sys.argv[1]), Path(sys.argv[2]))))
