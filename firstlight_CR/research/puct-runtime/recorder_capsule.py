"""Incremental recorder state for root-replay search.

The root owns immutable history.  A branch records only indexed replacements,
new tail rows, and its mutable execution bookkeeping.  This module deliberately
does not know the recorder's row model, so the same contract can be audited by
the real event recorder and by small deterministic fixtures.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import math
from typing import Any, Mapping


def _copy(value: Any) -> Any:
    return deepcopy(value)


def _equal(left: Any, right: Any) -> bool:
    try:
        import torch
        if isinstance(left, torch.Tensor) or isinstance(right, torch.Tensor):
            return isinstance(left, torch.Tensor) and isinstance(right, torch.Tensor) and torch.equal(left, right)
    except ImportError:
        pass
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return set(left) == set(right) and all(_equal(left[key], right[key]) for key in left)
    if is_dataclass(left) and not isinstance(left, type):
        return all(_equal(getattr(left, field.name), getattr(right, field.name))
                   for field in fields(left))
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    try:
        result = left == right
    except (TypeError, ValueError, RuntimeError):
        return False
    if isinstance(result, bool):
        return result
    try:
        import torch
        if isinstance(result, torch.Tensor):
            return bool(torch.all(result).item())
    except ImportError:
        pass
    all_method = getattr(result, "all", None)
    if callable(all_method):
        result = all_method()
    try:
        return bool(result)
    except (TypeError, ValueError, RuntimeError):
        return False


def _digest(value: Any) -> str:
    def stable(item: Any) -> Any:
        try:
            import torch
            if isinstance(item, torch.Tensor):
                tensor = item.detach().cpu().contiguous()
                if tensor.is_floating_point() and not bool(torch.isfinite(tensor).all()):
                    raise ValueError("semantic digest rejects non-finite tensors")
                raw = tensor.view(torch.uint8).numpy().tobytes()
                return {"tensor_dtype": str(tensor.dtype), "shape": list(tensor.shape),
                        "sha256": hashlib.sha256(raw).hexdigest()}
        except ImportError:
            pass
        if is_dataclass(item) and not isinstance(item, type):
            return {"dataclass": f"{type(item).__module__}:{type(item).__qualname__}",
                    "fields": {field.name: stable(getattr(item, field.name)) for field in fields(item)}}
        if isinstance(item, Mapping):
            return {str(key): stable(value) for key, value in sorted(item.items(), key=lambda pair: repr(pair[0]))}
        if isinstance(item, (tuple, list)):
            return [stable(value) for value in item]
        if isinstance(item, set):
            return sorted((stable(value) for value in item), key=repr)
        return item

    payload = json.dumps(stable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def semantic_digest(value: Any) -> str:
    return _digest(value)


def fast_semantic_digest(value: Any) -> str:
    """Hash semantic state while using a faster canonical JSON encoder."""
    try:
        import torch
    except ImportError:
        torch = None

    def stable(item: Any) -> Any:
        if torch is not None and isinstance(item, torch.Tensor):
            tensor = item.detach().cpu().contiguous()
            if tensor.is_floating_point() and not bool(torch.isfinite(tensor).all()):
                raise ValueError("semantic digest rejects non-finite tensors")
            raw = tensor.view(torch.uint8).numpy().tobytes()
            return {"tensor_dtype": str(tensor.dtype), "shape": list(tensor.shape),
                    "sha256": hashlib.sha256(raw).hexdigest()}
        if is_dataclass(item) and not isinstance(item, type):
            return {"dataclass": f"{type(item).__module__}:{type(item).__qualname__}",
                    "fields": {field.name: stable(getattr(item, field.name))
                               for field in fields(item)}}
        if isinstance(item, Mapping):
            return {str(key): stable(child)
                    for key, child in sorted(item.items(), key=lambda pair: repr(pair[0]))}
        if isinstance(item, (tuple, list)):
            return [stable(child) for child in item]
        if isinstance(item, set):
            return sorted((stable(child) for child in item), key=repr)
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("semantic digest rejects non-finite values")
        return item

    normalized = stable(value)
    try:
        import orjson
        encoded = orjson.dumps(normalized, option=orjson.OPT_SORT_KEYS)
    except ImportError:
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RootRecorderCapsule:
    rows_prefix: Mapping[int, tuple[Any, ...]]
    frames_prefix: Mapping[int, tuple[Any, ...]]
    row_masks_prefix: Mapping[int, tuple[Any, ...]]
    prefix_digest: str
    logical_lengths: tuple[int, int, int]
    runtime: Mapping[str, Any]

    @classmethod
    def from_recorder(cls, recorder: Any) -> "RootRecorderCapsule":
        rows = {owner: tuple(_copy(values)) for owner, values in recorder.rows.items()}
        frames = {owner: tuple(_copy(values)) for owner, values in recorder._frames.items()}
        masks = {owner: tuple(_copy(values)) for owner, values in recorder._row_masks.items()}
        prefix = (rows, frames, masks)
        return cls(
            rows_prefix=rows,
            frames_prefix=frames,
            row_masks_prefix=masks,
            prefix_digest=_digest(prefix),
            logical_lengths=(sum(map(len, rows.values())), sum(map(len, frames.values())),
                             sum(map(len, masks.values()))),
            runtime=_copy(_runtime(recorder)),
        )

    def audit(self) -> None:
        if _digest((self.rows_prefix, self.frames_prefix, self.row_masks_prefix)) != self.prefix_digest:
            raise ValueError("root recorder capsule was mutated")


@dataclass(frozen=True)
class RecorderBranchDelta:
    appended_rows: Mapping[int, tuple[Any, ...]]
    appended_frames: Mapping[int, tuple[Any, ...]]
    appended_row_masks: Mapping[int, tuple[Any, ...]]
    row_replacements: Mapping[int, Mapping[int, Any]]
    frame_replacements: Mapping[int, Mapping[int, Any]]
    row_mask_replacements: Mapping[int, Mapping[int, Any]]
    mutable_runtime_state: Mapping[str, Any]

    @classmethod
    def from_recorder(cls, recorder: Any, capsule: RootRecorderCapsule) -> "RecorderBranchDelta":
        current = (recorder.rows, recorder._frames, recorder._row_masks)
        prefixes = (capsule.rows_prefix, capsule.frames_prefix, capsule.row_masks_prefix)
        tails = []
        for values_by_owner, prefix_by_owner in zip(current, prefixes):
            tail = {}
            for owner, values in values_by_owner.items():
                prefix = prefix_by_owner.get(owner, ())
                tail[owner] = tuple(_copy(value) for value in values[len(prefix):])
            tails.append(tail)
        row_replacements = _owner_replacements(current[0], prefixes[0])
        frame_replacements = _owner_replacements(current[1], prefixes[1])
        mask_replacements = _owner_replacements(current[2], prefixes[2])
        return cls(
            appended_rows=tails[0], appended_frames=tails[1], appended_row_masks=tails[2],
            row_replacements=row_replacements, frame_replacements=frame_replacements,
            row_mask_replacements=mask_replacements,
            mutable_runtime_state=_copy(_runtime(recorder)),
        )

    def materialize_logical_history(self, capsule: RootRecorderCapsule) -> dict[str, Any]:
        return {
            "rows": _materialize(capsule.rows_prefix, self.row_replacements, self.appended_rows),
            "frames": _materialize(capsule.frames_prefix, self.frame_replacements, self.appended_frames),
            "row_masks": _materialize(capsule.row_masks_prefix, self.row_mask_replacements,
                                       self.appended_row_masks),
            "runtime": _copy(self.mutable_runtime_state),
        }

    def digest(self, capsule: RootRecorderCapsule) -> str:
        return _digest({
            "prefix_digest": capsule.prefix_digest,
            "appended_rows": self.appended_rows,
            "appended_frames": self.appended_frames,
            "appended_row_masks": self.appended_row_masks,
            "row_replacements": self.row_replacements,
            "frame_replacements": self.frame_replacements,
            "row_mask_replacements": self.row_mask_replacements,
            "mutable_runtime_state": self.mutable_runtime_state,
        })


@dataclass(frozen=True)
class SearchRecorderState:
    capsule: RootRecorderCapsule
    delta: RecorderBranchDelta

    def semantic_identity(self) -> dict[str, str]:
        return {
            "prefix_digest": self.capsule.prefix_digest,
            "branch_digest": self.delta.digest(self.capsule),
        }


def _owner_replacements(current: Mapping[int, list[Any]], prefixes: Mapping[int, tuple[Any, ...]]) -> dict[int, dict[int, Any]]:
    result = {}
    for owner, values in current.items():
        prefix = prefixes.get(owner, ())
        result[owner] = {index: _copy(value) for index, value in enumerate(values[:len(prefix)])
                         if not _equal(value, prefix[index])}
    return result


def _materialize(prefixes: Mapping[int, tuple[Any, ...]], replacements: Mapping[int, Mapping[int, Any]],
                 appended: Mapping[int, tuple[Any, ...]]) -> dict[int, list[Any]]:
    result = {}
    for owner, prefix in prefixes.items():
        values = [_copy(value) for value in prefix]
        for index, value in replacements.get(owner, {}).items():
            if not 0 <= index < len(values):
                raise ValueError("recorder replacement index is outside the root prefix")
            values[index] = _copy(value)
        values.extend(_copy(value) for value in appended.get(owner, ()))
        result[owner] = values
    return result


def _runtime(recorder: Any) -> dict[str, Any]:
    return {
        "pending": recorder._pending,
        "submitted": recorder._submitted,
        "action_ids": recorder._action_ids,
        "terminal": recorder._terminal,
    }


def restore_recorder(recorder: Any, capsule: RootRecorderCapsule, delta: RecorderBranchDelta) -> None:
    materialized = delta.materialize_logical_history(capsule)
    recorder._rows = materialized["rows"]
    recorder._frames = materialized["frames"]
    recorder._row_masks = materialized["row_masks"]
    runtime = materialized["runtime"]
    recorder._pending = runtime["pending"]
    recorder._submitted = runtime["submitted"]
    recorder._action_ids = runtime["action_ids"]
    recorder._terminal = runtime["terminal"]
