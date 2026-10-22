"""Compact action identities and priors for the PUCT candidate path.

The legacy search path uses canonical action strings as its internal key.  The
packed path keeps the same decode order and tie-break contract while carrying
only integer identities and numeric prior mass through the hot loop.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from dataclasses import field
from typing import Any, Iterable, Mapping, Sequence

import torch


@dataclass(frozen=True, slots=True, eq=False)
class PackedActionSpace:
    kind: Any
    card_row: Any
    cell: Any
    delay: Any
    ability_source: Any
    canonical_tie_rank: Any
    semantic_sha256: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        fields = (self.kind, self.card_row, self.cell, self.delay,
                  self.ability_source, self.canonical_tie_rank)
        size = len(self.kind)
        if any(len(field) != size for field in fields):
            raise ValueError("packed action fields must have equal lengths")
        if isinstance(self.canonical_tie_rank, torch.Tensor):
            expected = torch.arange(size, dtype=self.canonical_tie_rank.dtype,
                                    device=self.canonical_tie_rank.device)
            if not torch.equal(self.canonical_tie_rank, expected):
                raise ValueError("packed action tie ranks must be canonical")
            if torch.any((self.kind < 0) | (self.kind > 2)):
                raise ValueError("packed action kind is invalid")
        else:
            if tuple(sorted(self.canonical_tie_rank)) != tuple(range(size)):
                raise ValueError("packed action tie ranks must be a permutation")
            if any(value not in (0, 1, 2) for value in self.kind):
                raise ValueError("packed action kind is invalid")
        digest = hashlib.sha256()
        for name, value in zip(
                ("kind", "card_row", "cell", "delay", "ability_source", "canonical_tie_rank"),
                fields):
            digest.update(name.encode())
            digest.update(b"\0")
            if isinstance(value, torch.Tensor):
                tensor = value.detach().cpu().contiguous()
                digest.update(str(tensor.dtype).encode())
                digest.update(json.dumps(list(tensor.shape), separators=(",", ":")).encode())
                digest.update(tensor.view(torch.uint8).numpy().tobytes())
            else:
                digest.update(json.dumps(list(value), separators=(",", ":")).encode())
            digest.update(b"\0")
        object.__setattr__(self, "semantic_sha256", digest.hexdigest())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PackedActionSpace) or len(self) != len(other):
            return False
        for left, right in zip(
            (self.kind, self.card_row, self.cell, self.delay,
             self.ability_source, self.canonical_tie_rank),
            (other.kind, other.card_row, other.cell, other.delay,
             other.ability_source, other.canonical_tie_rank),
        ):
            if isinstance(left, torch.Tensor) or isinstance(right, torch.Tensor):
                if not isinstance(left, torch.Tensor) or not isinstance(right, torch.Tensor):
                    return False
                if not torch.equal(left, right):
                    return False
            elif left != right:
                return False
        return True

    def __len__(self) -> int:
        return len(self.kind)

    def decode_one(self, index: int) -> str:
        index = int(index)
        if not 0 <= index < len(self):
            raise IndexError("packed action index is outside the action space")
        kind = int(self.kind[index])
        if kind == 0:
            return "wait"
        if kind == 1:
            return f"play:{int(self.card_row[index])}:{int(self.cell[index])}:{int(self.delay[index])}"
        return f"ability:{int(self.ability_source[index])}:{int(self.delay[index])}"

    def decode_many(self, indices: Iterable[int]) -> tuple[str, ...]:
        return tuple(self.decode_one(index) for index in indices)

    def canonical_keys(self) -> tuple[str, ...]:
        return self.decode_many(range(len(self)))


@dataclass(frozen=True, slots=True)
class PackedPrior:
    actions: PackedActionSpace
    mass: Any

    def __post_init__(self) -> None:
        if len(self.mass) != len(self.actions):
            raise ValueError("packed prior mass does not match action space")
        if isinstance(self.mass, torch.Tensor):
            if self.mass.ndim != 1 or torch.any(self.mass < 0):
                raise ValueError("packed prior mass must be a non-negative vector")
        elif any(value < 0 for value in self.mass):
            raise ValueError("packed prior mass must be non-negative")

    def normalized(self, *, top_k: int | None = None, temperature: float = 1.0) -> "PackedPrior":
        if temperature <= 0:
            raise ValueError("packed prior temperature must be positive")
        mass = torch.as_tensor(self.mass)
        if mass.ndim != 1 or not len(mass):
            raise ValueError("packed prior requires a non-empty one-dimensional mass vector")
        powered = mass.pow(1.0 / temperature)
        if top_k is not None:
            order = torch.argsort(powered, descending=True, stable=True)[:int(top_k)]
            selected = powered.index_select(0, order)
            normalized = torch.zeros_like(powered)
            total = selected.sum()
            if not bool(total):
                selected = torch.ones_like(selected)
                total = selected.sum()
            normalized.scatter_(0, order, selected / total)
        else:
            total = powered.sum()
            if not bool(total):
                normalized = torch.full_like(powered, 1.0 / len(self.actions))
            else:
                normalized = powered / total
        # Selection is Python-driven and repeatedly reads individual masses.
        # Keep the immutable search vector on host memory to avoid an
        # accelerator synchronization for every selected action.
        return PackedPrior(self.actions, normalized.detach().to(device="cpu"))

    def to_mapping(self) -> dict[str, float]:
        return {self.actions.decode_one(index): float(value)
                for index, value in enumerate(self.mass) if float(value) > 0}


def packed_space_from_keys(keys: Sequence[str]) -> PackedActionSpace:
    kind, card_row, cell, delay, ability_source = [], [], [], [], []
    for key in keys:
        fields = key.split(":")
        if key == "wait":
            values = (0, -1, -1, -1, -1)
        elif len(fields) == 4 and fields[0] == "play":
            values = (1, int(fields[1]), int(fields[2]), int(fields[3]), -1)
        elif len(fields) == 3 and fields[0] == "ability":
            values = (2, -1, -1, int(fields[2]), int(fields[1]))
        else:
            raise ValueError(f"invalid canonical action key: {key}")
        for target, value in zip((kind, card_row, cell, delay, ability_source), values):
            target.append(value)
    return PackedActionSpace(tuple(kind), tuple(card_row), tuple(cell), tuple(delay),
                             tuple(ability_source), tuple(range(len(keys))))
