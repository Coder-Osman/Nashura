from copy import deepcopy
import hashlib
import io
import math
from pathlib import Path
import random

import numpy as np
import torch

from ..fair_search.contracts import require_sha, semantic_digest
from ..fair_search.storage import torch_bytes
from ..v4.config import ModelConfigV4
from .contracts import IMPLEMENTATION, exact_fields, integer
from .model import ConditionalPolicyValue
from .replay import FrozenReplayCursor
from .storage import durable_write


SCHEMA = "AZJointCheckpoint/v1"
BINDINGS = {"source_a_sha", "migration_adapter_sha", "active_champion_sha",
    "search_protocol_sha", "direct_controller_config_sha", "fair_observation_sha",
    "action_protocol_sha", "engine_protocol_sha"}


def cpu_state(value):
    if isinstance(value, torch.Tensor):
        if value.is_floating_point() and not bool(torch.isfinite(value).all()):
            raise ValueError("nonfinite checkpoint tensor")
        return value.detach().cpu().clone()
    if isinstance(value, dict):
        return {key: cpu_state(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(cpu_state(item) for item in value)
    if type(value) is float and not math.isfinite(value):
        raise ValueError("nonfinite checkpoint scalar")
    if value is None or type(value) in (int, float, bool, str):
        return value
    raise ValueError("unsupported checkpoint value")


def capture_rng():
    numpy_state = np.random.get_state()
    return dict(python=random.getstate(), numpy=dict(algorithm=numpy_state[0],
        keys=numpy_state[1].tolist(), position=numpy_state[2], has_gauss=numpy_state[3], cached=numpy_state[4]),
        cpu=torch.get_rng_state(), cuda=torch.cuda.get_rng_state_all() if torch.cuda.is_available() else [])


def validate_rng(state, *, topology=True):
    exact_fields(state, {"python", "numpy", "cpu", "cuda"}, "trainer RNG")
    random.Random().setstate(state["python"])
    torch.Generator().set_state(state["cpu"])
    numpy = state["numpy"]
    exact_fields(numpy, {"algorithm", "keys", "position", "has_gauss", "cached"}, "numpy RNG")
    np.random.RandomState().set_state((numpy["algorithm"], np.asarray(numpy["keys"], dtype=np.uint32),
        numpy["position"], numpy["has_gauss"], numpy["cached"]))
    if topology and len(state["cuda"]) != (torch.cuda.device_count() if torch.cuda.is_available() else 0):
        raise ValueError("CUDA RNG topology differs on resume")
    if topology:
        for index, value in enumerate(state["cuda"]):
            torch.Generator(device=f"cuda:{index}").set_state(value)


def restore_rng(state):
    validate_rng(state)
    numpy = state["numpy"]
    random.setstate(state["python"])
    np.random.set_state((numpy["algorithm"], np.asarray(numpy["keys"], dtype=np.uint32),
        numpy["position"], numpy["has_gauss"], numpy["cached"]))
    torch.set_rng_state(state["cpu"])
    if state["cuda"]:
        torch.cuda.set_rng_state_all(state["cuda"])


def validate_optimizer(optimizer, model):
    if type(optimizer) is not torch.optim.AdamW or len(optimizer.param_groups) != 1:
        raise ValueError("FP32 single-group AdamW required")
    group = optimizer.param_groups[0]
    parameters = list(model.parameters())
    if (not math.isfinite(group["lr"]) or group["lr"] <= 0
            or not math.isfinite(group["eps"]) or group["eps"] <= 0
            or not math.isfinite(group["weight_decay"]) or group["weight_decay"] < 0
            or len(group["betas"]) != 2 or any(not 0 <= beta < 1 for beta in group["betas"])):
        raise ValueError("invalid AdamW hyperparameters")
    if len(group["params"]) != len(parameters) or any(left is not right for left, right in zip(group["params"], parameters)):
        raise ValueError("optimizer parameter order differs from model")
    if any(parameter.dtype != torch.float32 or parameter.grad is not None for parameter in parameters):
        raise ValueError("checkpoint only at clean FP32 optimizer-step boundaries")
    allowed = set(parameters)
    for parameter, state in optimizer.state.items():
        if parameter not in allowed:
            raise ValueError("foreign optimizer parameter")
        if not state:
            continue
        required = {"step", "exp_avg", "exp_avg_sq"} | ({"max_exp_avg_sq"} if group["amsgrad"] else set())
        exact_fields(state, required, "AdamW state")
        step = state["step"]
        if step.numel() != 1 or float(step) < 0 or float(step) != int(step):
            raise ValueError("invalid AdamW step")
        for key in required - {"step"}:
            if state[key].shape != parameter.shape or state[key].dtype != parameter.dtype:
                raise ValueError("optimizer moment shape/dtype differs")
            if key.endswith("sq") and bool((state[key] < 0).any()):
                raise ValueError("negative optimizer second moment")
    cpu_state(optimizer.state_dict())


def validate_bindings(bindings):
    exact_fields(bindings, BINDINGS, "checkpoint provenance")
    for value in bindings.values():
        require_sha(value)


def validate_dataset_bindings(bindings, store):
    mapping = dict(engine_protocol_sha="engine_protocol_sha", action_protocol_sha="action_protocol_sha",
        search_protocol_sha="search_protocol_sha", fair_observation_sha="observation_contract_sha")
    if any(bindings[key] != store.manifest["identity"][field] for key, field in mapping.items()):
        raise ValueError("checkpoint and replay protocol bindings differ")


def validate_optimizer_steps(optimizer, steps):
    completed = [int(state["step"]) for state in optimizer.state.values() if state]
    if (steps and (not completed or max(completed) != steps)) or any(value > steps for value in completed):
        raise ValueError("optimizer moments were reset or belong to a different update")


def save_checkpoint(path, model, optimizer, sampler, *, bindings, optimizer_steps, batch_size=256, gradient_clip=1.):
    if not isinstance(model, ConditionalPolicyValue) or not isinstance(sampler, FrozenReplayCursor):
        raise TypeError("independent FAIR final model and replay cursor required")
    validate_bindings(bindings)
    validate_dataset_bindings(bindings, sampler.store)
    validate_optimizer(optimizer, model)
    integer(optimizer_steps, 0, "optimizer_steps")
    validate_optimizer_steps(optimizer, optimizer_steps)
    integer(batch_size, 1, "batch_size")
    if (not math.isfinite(gradient_clip) or gradient_clip <= 0
            or sampler.consumed != optimizer_steps * batch_size):
        raise ValueError("checkpoint budget must end at a complete optimizer update")
    replay_state = sampler.state_dict()
    sampler.load_state_dict(replay_state)
    network = cpu_state(model.state_dict())
    optimizer_state = cpu_state(optimizer.state_dict())
    rng = capture_rng()
    payload = dict(schema=SCHEMA, implementation=IMPLEMENTATION, role="LEARNER",
        architecture=model.architecture, architecture_sha=semantic_digest(model.architecture),
        encoder_catalog_sha=semantic_digest(model.architecture["catalogs"]),
        network=network, network_sha=semantic_digest(network),
        optimizer=optimizer_state, optimizer_sha=semantic_digest(optimizer_state),
        optimizer_parameter_names=[name for name, _ in model.named_parameters()],
        trainable_names=[name for name, parameter in model.named_parameters() if parameter.requires_grad],
        training={name: module.training for name, module in model.named_modules()}, rng=rng, rng_sha=semantic_digest(rng),
        replay=replay_state, replay_sha=semantic_digest(replay_state), bindings=dict(bindings),
        trainer=dict(state="RUNNING", optimizer_steps=optimizer_steps, batch_size=batch_size,
            gradient_clip=float(gradient_clip), sample_budget=sampler.budget, consumed=sampler.consumed,
            sample_credit=sampler.budget - sampler.consumed, gradient_scaler=None))
    envelope = dict(schema="FairFinalCheckpointEnvelope/v1", sha256=semantic_digest(payload), payload=payload)
    data = torch_bytes(envelope)
    durable_write(path, data)
    return dict(file_sha=hashlib.sha256(data).hexdigest(), network_sha=payload["network_sha"])


def read_checkpoint(path, *, expected_sha, bindings):
    validate_bindings(bindings)
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != require_sha(expected_sha):
        raise ValueError("checkpoint file checksum differs")
    envelope = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
    exact_fields(envelope, {"schema", "sha256", "payload"}, "checkpoint envelope")
    if envelope["schema"] != "FairFinalCheckpointEnvelope/v1" or semantic_digest(envelope["payload"]) != envelope["sha256"]:
        raise ValueError("checkpoint semantic checksum differs")
    payload = envelope["payload"]
    exact_fields(payload, {"schema", "implementation", "role", "architecture", "architecture_sha",
        "encoder_catalog_sha", "network", "network_sha", "optimizer", "optimizer_sha",
        "optimizer_parameter_names", "trainable_names", "training", "rng", "rng_sha",
        "replay", "replay_sha", "bindings", "trainer"}, "checkpoint")
    if (payload["schema"] != SCHEMA or payload["implementation"] != IMPLEMENTATION
            or payload["role"] != "LEARNER" or payload["bindings"] != bindings):
        raise ValueError("checkpoint version, role or protocol bindings differ")
    for field in ("architecture", "network", "optimizer", "rng", "replay"):
        if semantic_digest(payload[field]) != payload[field + "_sha"]:
            raise ValueError("checkpoint component checksum differs: " + field)
    if semantic_digest(payload["architecture"]["catalogs"]) != payload["encoder_catalog_sha"]:
        raise ValueError("checkpoint encoder catalog differs")
    trainer = payload["trainer"]
    exact_fields(trainer, {"state", "optimizer_steps", "batch_size", "gradient_clip",
        "sample_budget", "consumed", "sample_credit", "gradient_scaler"}, "trainer state")
    if trainer["state"] not in ("RUNNING", "PAUSED") or trainer["gradient_scaler"] is not None:
        raise ValueError("quarantined or mixed-precision checkpoint is not resumable")
    for field in ("optimizer_steps", "sample_budget", "consumed", "sample_credit"):
        integer(trainer[field], 0, field)
    integer(trainer["batch_size"], 1, "batch_size")
    if (trainer["consumed"] != trainer["optimizer_steps"] * trainer["batch_size"]
            or trainer["sample_budget"] != trainer["consumed"] + trainer["sample_credit"]
            or not math.isfinite(trainer["gradient_clip"]) or trainer["gradient_clip"] <= 0):
        raise ValueError("trainer accounting differs")
    cpu_state(payload)
    return payload


def resume_checkpoint(path, *, expected_sha, bindings, store, bundle=None, device="cpu"):
    original_rng = capture_rng()
    try:
        payload = read_checkpoint(path, expected_sha=expected_sha, bindings=bindings)
        validate_dataset_bindings(bindings, store)
        validate_rng(payload["rng"])
        replay = payload["replay"]
        sampler = FrozenReplayCursor(store, replay["matches"], seed=0, sample_budget=replay["budget"])
        sampler.load_state_dict(replay)
        if sampler.consumed != payload["trainer"]["consumed"] or sampler.budget != payload["trainer"]["sample_budget"]:
            raise ValueError("replay and trainer budget disagree")
        model = ConditionalPolicyValue(bundle=bundle, encoder_config=ModelConfigV4(**payload["architecture"]["encoder"]))
        if semantic_digest(model.architecture) != payload["architecture_sha"]:
            raise ValueError("checkpoint model/catalog architecture differs")
        current = model.state_dict()
        if set(current) != set(payload["network"]) or any(current[name].shape != value.shape
                or current[name].dtype != value.dtype for name, value in payload["network"].items()):
            raise ValueError("checkpoint parameter/buffer shape or dtype differs")
        model.load_state_dict(payload["network"], strict=True)
        model.to(device)
        names = [name for name, _ in model.named_parameters()]
        trainable = payload["trainable_names"]
        if (payload["optimizer_parameter_names"] != names or len(trainable) != len(set(trainable))
                or not set(trainable) <= set(names)):
            raise ValueError("checkpoint optimizer parameter identity differs")
        for name, parameter in model.named_parameters():
            parameter.requires_grad_(name in trainable)
        modules = dict(model.named_modules())
        exact_fields(payload["training"], modules, "module train/eval modes")
        if any(type(value) is not bool for value in payload["training"].values()):
            raise ValueError("invalid module train/eval modes")
        for name, module in modules.items():
            module.training = payload["training"][name]
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
        optimizer.load_state_dict(payload["optimizer"])
        validate_optimizer(optimizer, model)
        validate_optimizer_steps(optimizer, payload["trainer"]["optimizer_steps"])
        restore_rng(payload["rng"])
        return model, optimizer, sampler, payload
    except Exception:
        restore_rng(original_rng)
        raise


def export_candidate(checkpoint_path, destination, *, expected_sha, bindings):
    payload = read_checkpoint(checkpoint_path, expected_sha=expected_sha, bindings=bindings)
    candidate = {key: deepcopy(payload[key]) for key in ("architecture", "architecture_sha",
        "encoder_catalog_sha", "network", "network_sha", "bindings")}
    candidate.update(schema="FairFinalCandidate/v1", implementation=IMPLEMENTATION,
        role="CANDIDATE", parent_checkpoint_sha=expected_sha)
    data = torch_bytes(candidate)
    durable_write(destination, data)
    return dict(file_sha=hashlib.sha256(data).hexdigest(), network_sha=payload["network_sha"])
