"""Stable digests for exactly the tensors delivered to the V4 policy.

Search-particle checks must compare model inputs, not transport/session
metadata from a FAIR observation.  The recursive encoder deliberately
supports only the TensorRecord dataclasses used by the V4 tensorizer.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
from typing import Any

import torch


def _append(value: Any, digest: "hashlib._Hash") -> None:
    if isinstance(value, torch.Tensor):
        tensor = value.detach().to(device="cpu").contiguous()
        digest.update(b"tensor\0")
        digest.update(str(tensor.dtype).encode("ascii"))
        digest.update(json.dumps(tuple(tensor.shape)).encode("ascii"))
        digest.update(tensor.view(torch.uint8).numpy().tobytes())
        return
    if is_dataclass(value):
        digest.update(f"dataclass:{type(value).__qualname__}\0".encode("utf-8"))
        for field in fields(value):
            digest.update(f"field:{field.name}\0".encode("utf-8"))
            _append(getattr(value, field.name), digest)
        return
    if value is None or isinstance(value, (bool, int, float, str)):
        digest.update(b"scalar\0")
        digest.update(json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        return
    if isinstance(value, tuple):
        digest.update(b"tuple\0")
        for item in value:
            _append(item, digest)
        return
    raise TypeError(f"unsupported policy-input field {type(value).__qualname__}")


def tensor_record_sha256(record: Any) -> str:
    """Hash values, dtypes and shapes of a nested V4 tensor record."""

    digest = hashlib.sha256()
    _append(record, digest)
    return digest.hexdigest()
