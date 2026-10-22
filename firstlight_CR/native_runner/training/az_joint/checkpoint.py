from dataclasses import asdict
import hashlib
import io
from pathlib import Path

import torch

from ..fair_search.contracts import semantic_digest, require_sha
from ..fair_search.model import rng_state, restore_rng
from ..fair_search.storage import atomic_write, torch_bytes
from ..v4.config import ModelConfigV4
from .config import JointConfig
from .model import JointPolicyValue


SCHEMA = "az-joint-checkpoint.v1"
MIGRATION_SCHEMA = "joint-migration-checkpoint.v1"


def save_checkpoint(path, model, optimizer, *, config, state, provenance, migration, replace=False):
    payload = dict(schema=MIGRATION_SCHEMA if migration else SCHEMA, architecture=model.architecture,
        config=asdict(config), config_sha256=config.sha256, model=model.state_dict(),
        optimizer=optimizer.state_dict(), rng=rng_state(), state=state, provenance=provenance)
    data = torch_bytes(payload)
    atomic_write(path, data, replace=replace)
    return hashlib.sha256(data).hexdigest()


def load_checkpoint(path, *, migration, device="cpu", config=None, expected_sha256=None, bundle=None):
    data = Path(path).read_bytes()
    identity = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and identity != require_sha(expected_sha256):
        raise ValueError("joint checkpoint identity differs")
    payload = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    expected = {"schema", "architecture", "config", "config_sha256", "model", "optimizer", "rng", "state", "provenance"}
    if (not isinstance(payload, dict) or set(payload) != expected
            or payload["schema"] != (MIGRATION_SCHEMA if migration else SCHEMA)):
        raise ValueError("joint checkpoint version boundary differs")
    saved_config = JointConfig(**payload["config"])
    if saved_config.sha256 != payload["config_sha256"] or (config is not None and config.sha256 != saved_config.sha256):
        raise ValueError("joint checkpoint configuration differs")
    model = JointPolicyValue(bundle=bundle, encoder_config=ModelConfigV4(**payload["architecture"]["encoder"]))
    if semantic_digest(model.architecture) != semantic_digest(payload["architecture"]):
        raise ValueError("joint semantic catalog or architecture differs")
    model.load_state_dict(payload["model"], strict=True)
    if any(not bool(torch.isfinite(tensor).all()) for tensor in model.state_dict().values() if tensor.is_floating_point()):
        raise ValueError("nonfinite joint model weights")
    return model.to(device), payload, identity


def new_optimizer(model, config):
    return torch.optim.AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)


def resume_checkpoint(path, *, migration, device="cpu", config=None, bundle=None):
    model, payload, identity = load_checkpoint(path, migration=migration, device=device, config=config, bundle=bundle)
    optimizer = new_optimizer(model, JointConfig(**payload["config"]))
    optimizer.load_state_dict(payload["optimizer"])
    restore_rng(payload["rng"])
    return model, optimizer, payload, identity


def export_initial(source, destination, *, acceptance, config=None):
    if acceptance.get("passed") is not True:
        raise ValueError("migration acceptance has not passed")
    config = config or JointConfig()
    model, payload, identity = load_checkpoint(source, migration=True, config=config)
    if acceptance.get("checkpoint_sha256") != identity:
        raise ValueError("acceptance refers to a different migration candidate")
    optimizer = new_optimizer(model, config)
    return save_checkpoint(destination, model, optimizer, config=config,
        state=dict(step=0, generation=0, sample_credit=0, replay=[], sampler=None),
        provenance=dict(migration_sha256=identity, migration=payload["provenance"], acceptance=acceptance),
        migration=False)
