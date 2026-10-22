"""Environment-independent event checkpoint persistence and architecture identity.

Legacy semantics are rejected unless ``initialization_only=True`` explicitly
opts into changed predictions and subsequent recalibration/finetuning. Unversioned
checkpoints must declare ``snapshot_only`` and ``model_scale``; missing values
require explicit caller overrides. Tensor shapes cannot identify these settings.
New checkpoints persist every config field and both input and action semantics.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import io
import json
import math
import os
import tempfile
import warnings
from pathlib import Path
from typing import Any, Mapping

import torch

from .event_config import EventPolicyConfigV1
from .event_model import EVENT_MODEL_VERSION, EventCardPolicyV1, EventModelConfigV1, pad_legacy_delay_head
from .v4.config import ModelConfigV4, scaled_model_config_v4
from .v4.factory import production_semantic_bundle
from .v4.model import UniversalCardPolicyV4

EVENT_CHECKPOINT_VERSION = "event-checkpoint.v2"
RESIDUAL_CHECKPOINT_VERSION = "event-checkpoint.v3"
RESIDUAL_MODEL_VERSION = "event-card-policy-model.v3"
_RESIDUAL_FIELDS = {"residual_blocks", "residual_hidden"}
EVENT_INPUT_SEMANTICS = "owner-neutral.v1"
EVENT_ACTION_SEMANTICS = "model-relative-cell.v1"
ABILITY_CHECKPOINT_VERSION = "event-checkpoint.v4"
ABILITY_MODEL_VERSION = "event-card-ability-policy-model.v1"
DELAYED_ABILITY_CHECKPOINT_VERSION = "event-checkpoint.v5"
DELAYED_ABILITY_MODEL_VERSION = "event-card-ability-policy-model.v2"
INITIALIZATION_KEY = "event_initialization"
CONTRACT_KEY = "event_checkpoint_contract"


@dataclass(frozen=True)
class LoadedEventCheckpoint:
    model: EventCardPolicyV1
    payload: dict[str, Any]
    checkpoint_sha256: str
    contract_sha256: str


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{name} must be an explicit boolean (true/false or 1/0), got {value!r}")


def _scale(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("model_scale must be a finite positive number")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("model_scale must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("model_scale must be a finite positive number")
    return result


def _config(cls: type, raw: Any, name: str):
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be a config mapping")
    expected = {field.name for field in fields(cls)}
    if set(raw) != expected:
        raise ValueError(f"{name} fields invalid: missing={sorted(expected - set(raw))}, unknown={sorted(set(raw) - expected)}")
    values = dict(raw)
    defaults = cls()
    for key, value in values.items():
        default = getattr(defaults, key)
        valid = True
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, int):
            valid = type(value) is int
        elif isinstance(default, float):
            valid = type(value) in (int, float) and math.isfinite(value)
        elif isinstance(default, tuple):
            valid = isinstance(value, (tuple, list)) and all(type(item) is int for item in value)
            if valid:
                values[key] = tuple(value)
        if not valid:
            raise ValueError(f"{name}.{key} has invalid type or value: {value!r}")
    try:
        return cls(**values)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {exc}") from exc


def _contract(backbone: ModelConfigV4, model: EventModelConfigV1, event: EventPolicyConfigV1) -> dict[str, Any]:
    model_config = asdict(model)
    residual = model.residual_blocks > 0
    ability = model.ability_actions
    if not model.ability_delay:
        model_config.pop("ability_delay")
    if not ability:
        model_config.pop("ability_actions")
    if not residual:
        for key in _RESIDUAL_FIELDS:
            model_config.pop(key)
    return {
        "version": DELAYED_ABILITY_CHECKPOINT_VERSION if model.ability_delay else ABILITY_CHECKPOINT_VERSION if ability else RESIDUAL_CHECKPOINT_VERSION if residual else EVENT_CHECKPOINT_VERSION,
        "model_version": DELAYED_ABILITY_MODEL_VERSION if model.ability_delay else ABILITY_MODEL_VERSION if ability else RESIDUAL_MODEL_VERSION if residual else EVENT_MODEL_VERSION,
        "input_semantics": EVENT_INPUT_SEMANTICS,
        "action_semantics": ("model-relative-cell-and-delayed-native-ability-event.v1" if model.ability_delay else "model-relative-cell-and-native-ability-event.v1" if ability else EVENT_ACTION_SEMANTICS),
        "backbone_config": asdict(backbone),
        "model_config": model_config,
        "event_config": asdict(event),
    }


def _contract_hash(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _resolve(payload: Mapping[str, Any], snapshot_override: Any, scale_override: Any, *, initialization_only: bool = False):
    if CONTRACT_KEY in payload:
        contract = payload[CONTRACT_KEY]
        if not isinstance(contract, Mapping):
            raise ValueError("invalid event_checkpoint_contract fields")
        legacy = (contract.get("version"), contract.get("model_version")) == (
            "event-checkpoint.v1", "event-card-policy-model.v1")
        current = (contract.get("version"), contract.get("model_version")) == (
            EVENT_CHECKPOINT_VERSION, EVENT_MODEL_VERSION)
        residual = (contract.get("version"), contract.get("model_version")) == (
            RESIDUAL_CHECKPOINT_VERSION, RESIDUAL_MODEL_VERSION)
        ability = (contract.get("version"), contract.get("model_version")) == (
            ABILITY_CHECKPOINT_VERSION, ABILITY_MODEL_VERSION)
        delayed_ability = (contract.get("version"), contract.get("model_version")) == (
            DELAYED_ABILITY_CHECKPOINT_VERSION, DELAYED_ABILITY_MODEL_VERSION)
        ability = ability or delayed_ability
        if not (current or residual or ability) and not (legacy and initialization_only):
            raise ValueError("unsupported event checkpoint/model contract version; legacy weights require initialization_only=True and recalibration/finetuning")
        expected = {"version", "model_version", "backbone_config", "model_config", "event_config"}
        if current or residual or ability:
            expected |= {"input_semantics", "action_semantics"}
            action_semantics = ("model-relative-cell-and-delayed-native-ability-event.v1" if delayed_ability
                                else "model-relative-cell-and-native-ability-event.v1" if ability else EVENT_ACTION_SEMANTICS)
            if contract.get("input_semantics") != EVENT_INPUT_SEMANTICS or contract.get("action_semantics") != action_semantics:
                raise ValueError("unsupported event input/action semantics")
        if set(contract) != expected:
            raise ValueError("invalid event_checkpoint_contract fields")
        backbone = _config(ModelConfigV4, contract["backbone_config"], "backbone_config")
        raw_model = contract["model_config"]
        if not isinstance(raw_model, Mapping):
            raise ValueError("model_config must be a mapping")
        raw_model = dict(raw_model)
        if delayed_ability:
            if raw_model.get("ability_delay") is not True:
                raise ValueError("v5 event contract must enable ability delays")
        else:
            if "ability_delay" in raw_model:
                raise ValueError("legacy event contract cannot declare ability delays")
            raw_model["ability_delay"] = False
        if ability:
            if raw_model.get("ability_actions") is not True:
                raise ValueError("v4 event contract must enable ability actions")
            residual = raw_model.get("residual_blocks", 0) > 0
        else:
            if "ability_actions" in raw_model:
                raise ValueError("legacy event contract cannot declare ability actions")
            raw_model["ability_actions"] = False
        if not residual:
            expected_model = {field.name for field in fields(EventModelConfigV1)} - _RESIDUAL_FIELDS
            if not isinstance(raw_model, Mapping) or set(raw_model) != expected_model:
                raise ValueError("model_config fields invalid for v1/v2")
            defaults = asdict(EventModelConfigV1())
            raw_model = {**raw_model, **{key: defaults[key] for key in _RESIDUAL_FIELDS}}
        model = _config(EventModelConfigV1, raw_model, "model_config")
        if residual and (model.residual_blocks < 1 or not backbone.snapshot_only):
            raise ValueError("v3 requires a stateless snapshot residual core")
        event = _config(EventPolicyConfigV1, contract["event_config"], "event_config")
        if snapshot_override is not None or scale_override is not None:
            raise ValueError("legacy overrides cannot be applied to a versioned checkpoint")
        if "snapshot_only" in payload and _bool(payload["snapshot_only"], "snapshot_only") != backbone.snapshot_only:
            raise ValueError("snapshot_only conflicts with backbone_config")
        if "model_scale" in payload:
            scaled = replace(scaled_model_config_v4(_scale(payload["model_scale"])), snapshot_only=backbone.snapshot_only)
            if scaled != backbone:
                raise ValueError("model_scale conflicts with backbone_config")
    else:
        if not initialization_only:
            raise ValueError("legacy checkpoint requires initialization_only=True and recalibration/finetuning")
        resolved = {}
        for key, override, parse in (("snapshot_only", snapshot_override, lambda v: _bool(v, "snapshot_only")), ("model_scale", scale_override, _scale)):
            if key not in payload and override is None:
                raise ValueError(f"legacy checkpoint missing {key} metadata; supply legacy_{key} explicitly after verifying the training configuration, or re-export with a full event checkpoint contract")
            resolved[key] = parse(payload[key] if key in payload else override)
            if key in payload and override is not None and parse(override) != resolved[key]:
                raise ValueError(f"legacy_{key} override conflicts with checkpoint metadata")
        backbone = replace(scaled_model_config_v4(resolved["model_scale"]), snapshot_only=resolved["snapshot_only"])
        def legacy_config(cls: type, key: str):
            raw = payload.get(key, {})
            if not isinstance(raw, Mapping):
                raise ValueError(f"legacy {key} must be a config mapping")
            return _config(cls, {**asdict(cls()), **raw}, key)

        model = legacy_config(EventModelConfigV1, "model_config")
        event = legacy_config(EventPolicyConfigV1, "event_config")
    return backbone, model, event


def _build_model(backbone_config: ModelConfigV4, model_config: EventModelConfigV1, event_config: EventPolicyConfigV1) -> EventCardPolicyV1:
    # build_event_model_v1 currently reads environment-only backbone settings.
    bundle = production_semantic_bundle()
    backbone = UniversalCardPolicyV4(
        bundle.card_catalog,
        ability_catalog=bundle.ability_catalog,
        entity_archetype_catalog=bundle.entity_archetype_catalog,
        effect_catalog=bundle.effect_catalog,
        mechanic_profile_catalog=bundle.mechanic_profile_catalog,
        config=backbone_config,
    )
    return EventCardPolicyV1(backbone, config=model_config, event_config=event_config)


def load_event_checkpoint(path: str | Path, *, device: torch.device | str = "cpu", legacy_snapshot_only: bool | str | None = None, legacy_model_scale: float | str | None = None, initialization_only: bool = False) -> LoadedEventCheckpoint:
    """Load strictly with saved semantics, never consulting process environment.

    Identity hashes the exact bytes deserialized. The contract hash identifies
    resolved configs, not weights. Returned model retains normal training mode;
    inference callers should call ``eval()`` themselves.
    """
    if type(initialization_only) is not bool:
        raise ValueError("initialization_only must be an explicit boolean")
    data = Path(path).read_bytes()
    payload = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), Mapping):
        raise ValueError("event checkpoint must contain a 'model' state dict")
    backbone, config, event = _resolve(payload, legacy_snapshot_only, legacy_model_scale, initialization_only=initialization_only)
    legacy = payload.get(CONTRACT_KEY, {}).get("version") not in {
        EVENT_CHECKPOINT_VERSION, RESIDUAL_CHECKPOINT_VERSION, ABILITY_CHECKPOINT_VERSION, DELAYED_ABILITY_CHECKPOINT_VERSION
    }
    initialization = payload.get(INITIALIZATION_KEY)
    if initialization is not None and (not isinstance(initialization, dict) or type(initialization.get("needs_recalibration_or_finetune")) is not bool):
        raise ValueError("invalid event initialization metadata")
    if initialization and initialization["needs_recalibration_or_finetune"] and not initialization_only:
        raise ValueError("checkpoint still needs recalibration/finetuning; use initialization_only=True for training")
    contract = _contract(backbone, config, event)
    contract_sha256 = _contract_hash(contract)
    source_contract_hash = _contract_hash(payload[CONTRACT_KEY]) if legacy and CONTRACT_KEY in payload else contract_sha256
    if "contract_sha256" in payload and payload["contract_sha256"] != source_contract_hash:
        raise ValueError("checkpoint contract_sha256 does not match resolved metadata")
    model = _build_model(backbone, config, event)
    try:
        model.load_state_dict(pad_legacy_delay_head(payload["model"], model), strict=True)
    except (RuntimeError, ValueError) as exc:
        raise ValueError(f"event checkpoint weights do not match declared configuration: {exc}") from exc
    model.to(device)
    if legacy:
        initialization = {
            "migrated_from": {"checkpoint_sha256": hashlib.sha256(data).hexdigest(),
                              "contract": payload.get(CONTRACT_KEY, "unversioned")},
            "needs_recalibration_or_finetune": True,
            "needs_evaluation": True,
        }
        # Old optimizer state and evaluation/rank claims are not portable.
        payload = {"model": model.state_dict(), CONTRACT_KEY: contract,
                   "contract_sha256": contract_sha256, INITIALIZATION_KEY: initialization}
    if initialization is not None:
        model.event_initialization = dict(initialization)
    if legacy:
        warnings.warn("Owner-neutral inputs and model-relative cells change expected behavior; legacy weights are initialization only and require recalibration/finetuning and new evaluation.", UserWarning, stacklevel=2)
    elif initialization and initialization["needs_recalibration_or_finetune"]:
        warnings.warn("Migrated checkpoint is initialization only and requires recalibration/finetuning and new evaluation.", UserWarning, stacklevel=2)
    return LoadedEventCheckpoint(model, payload, hashlib.sha256(data).hexdigest(), contract_sha256)


def complete_event_initialization(model: EventCardPolicyV1) -> None:
    """Record completed finetuning after actual optimizer updates, not evaluation.

    The training caller must only invoke this after a successful nonzero update.
    Provenance and the requirement for fresh evaluation remain attached.
    """
    metadata = getattr(model, "event_initialization", None)
    if metadata is not None:
        model.event_initialization = {**metadata, "needs_recalibration_or_finetune": False,
                                      "needs_evaluation": True}


def event_checkpoint_payload(model: EventCardPolicyV1, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Capture actual model configs, never environment hints or guessed scale.

    ``extra`` is for optimizer/training metadata, not reserved contract fields.
    """
    payload = dict(extra or {})
    reserved = {INITIALIZATION_KEY, "model", CONTRACT_KEY, "contract_sha256", "checkpoint_sha256", "snapshot_only", "model_scale", "model_config", "event_config", "backbone_config"}
    if reserved.intersection(payload):
        raise ValueError(f"extra contains reserved checkpoint fields: {sorted(reserved.intersection(payload))}")
    contract = _contract(model.backbone.config, model.config, model.event_config)
    _resolve({CONTRACT_KEY: contract}, None, None)
    if hasattr(model, "event_initialization"):
        payload[INITIALIZATION_KEY] = dict(model.event_initialization)
    payload.update(model=model.state_dict(), event_checkpoint_contract=contract, contract_sha256=_contract_hash(contract))
    return payload


def save_event_checkpoint(path: str | Path, model: EventCardPolicyV1, *, extra: Mapping[str, Any] | None = None) -> str:
    """Save the versioned payload and return the written file's SHA-256."""
    buffer = io.BytesIO()
    torch.save(event_checkpoint_payload(model, extra=extra), buffer)
    data = buffer.getvalue()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()
