"""JSON tensor codec, deliberately independent of native engines and content packs.

The wire format is unchanged. Only decode artifacts from trusted experiment storage.
"""
from __future__ import annotations
from dataclasses import fields, is_dataclass
from enum import Enum
from importlib import import_module
import math
from typing import Any, Mapping
import torch

def _type_name(value: type) -> str:
    return f"{value.__module__}:{value.__qualname__}"


def _resolve_type(name: str) -> type:
    module_name, separator, qualname = name.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError("invalid tagged rollout type")
    result: Any = import_module(module_name)
    for part in qualname.split("."):
        result = getattr(result, part)
    if not isinstance(result, type):
        raise ValueError("tagged rollout type is not a class")
    return result


def _encode(value: Any) -> Any:
    """Encode mutable policy state into deterministic JSON-safe values."""

    if isinstance(value, Enum):
        return {"__type__": "enum", "class": _type_name(type(value)), "value": _encode(value.value)}
    if isinstance(value, torch.Tensor):
        return {
            "__type__": "tensor",
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "values": value.tolist(),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": "dataclass",
            "class": _type_name(type(value)),
            "fields": {field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda pair: repr(pair[0]))
        return {"__type__": "mapping", "items": [[_encode(key), _encode(item)] for key, item in items]}
    if isinstance(value, tuple):
        return {"__type__": "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return {"__type__": "list", "items": [_encode(item) for item in value]}
    if isinstance(value, set):
        return {"__type__": "set", "items": sorted((_encode(item) for item in value), key=repr)}
    if isinstance(value, frozenset):
        return {"__type__": "frozenset", "items": sorted((_encode(item) for item in value), key=repr)}
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("rollout state contains non-finite float")
    return value


def _torch_dtype(name: str) -> torch.dtype:
    for dtype in (torch.float16, torch.float32, torch.float64, torch.bfloat16,
                  torch.int8, torch.int16, torch.int32, torch.int64,
                  torch.uint8, torch.bool):
        if str(dtype) == name:
            return dtype
    raise ValueError(f"unsupported encoded rollout tensor dtype: {name}")


def _decode(value: Any) -> Any:
    if not isinstance(value, Mapping) or "__type__" not in value:
        return value
    kind = value["__type__"]
    if kind == "enum":
        cls = _resolve_type(str(value["class"]))
        if not issubclass(cls, Enum):
            raise ValueError("tagged rollout enum is not an Enum")
        return cls(_decode(value["value"]))
    if kind == "tensor":
        tensor = torch.tensor(value["values"], dtype=_torch_dtype(str(value["dtype"])))
        shape = value["shape"]
        if any(type(size) is not int or size < 0 for size in shape):
            raise ValueError("encoded rollout tensor shape mismatch")
        # Nested lists carry no dimensions beyond their first empty axis.
        if tensor.numel() == 0 and math.prod(shape) == 0:
            tensor = tensor.reshape(shape)
        if list(tensor.shape) != list(value["shape"]):
            raise ValueError("encoded rollout tensor shape mismatch")
        return tensor
    if kind == "mapping":
        return {_decode(key): _decode(item) for key, item in value["items"]}
    if kind == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if kind == "list":
        return [_decode(item) for item in value["items"]]
    if kind in {"set", "frozenset"}:
        items = (_decode(item) for item in value["items"])
        return set(items) if kind == "set" else frozenset(items)
    if kind == "dataclass":
        cls = _resolve_type(str(value["class"]))
        fields_value = {name: _decode(item) for name, item in value["fields"].items()}
        return cls(**fields_value)
    raise ValueError(f"unsupported encoded rollout state type: {kind}")

