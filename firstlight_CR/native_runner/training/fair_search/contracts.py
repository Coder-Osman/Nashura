from dataclasses import asdict, dataclass, fields, is_dataclass
from collections.abc import Sequence
import hashlib
import json
import math
from typing import Mapping

import torch

from ..v4 import tensors as tensor_types


INFORMATION_DIGEST_SCHEMA = "fair-information-history-chain.v2"


def require_sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("invalid SHA-256 identity")
    return value


def pack(value):
    if isinstance(value, torch.Tensor):
        if value.is_floating_point() and not bool(torch.isfinite(value).all()):
            raise ValueError("nonfinite tensor in FAIR data")
        return value.detach().cpu().clone()
    if is_dataclass(value) and isinstance(value, tensor_types.TensorRecordV4):
        return {"record_type": type(value).__name__,
                "fields": {item.name: pack(getattr(value, item.name)) for item in fields(value)}}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("wire mapping keys must be strings")
        return {key: pack(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [pack(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("unsupported FAIR wire value")


def unpack(value):
    if isinstance(value, dict) and "record_type" in value:
        if set(value) != {"record_type", "fields"}:
            raise ValueError("tensor record fields differ")
        record_type = getattr(tensor_types, value["record_type"], None)
        if not isinstance(record_type, type) or not issubclass(record_type, tensor_types.TensorRecordV4):
            raise ValueError("unknown tensor record type")
        if set(value["fields"]) != {item.name for item in fields(record_type)}:
            raise ValueError("tensor record structure differs")
        return record_type(**{key: unpack(item) for key, item in value["fields"].items()})
    if isinstance(value, dict):
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value


def semantic_digest(value):
    digest = hashlib.sha256()

    def visit(item):
        if isinstance(item, DrawHistory):
            digest.update(b"fair-sampled-draw-history.v1")
            visit((item.initial, item.bindings, item.phases))
        elif isinstance(item, FrozenHistory):
            digest.update(b"fair-history-chain.v1")
            digest.update(str(len(item)).encode())
            digest.update(item.digest)
        elif isinstance(item, torch.Tensor):
            tensor = item.detach().cpu().contiguous()
            digest.update(json.dumps([str(tensor.dtype), list(tensor.shape)]).encode())
            digest.update(tensor.reshape(-1).view(torch.uint8).numpy().tobytes())
        elif is_dataclass(item):
            digest.update(type(item).__name__.encode())
            for field in fields(item):
                digest.update(field.name.encode())
                visit(getattr(item, field.name))
        elif isinstance(item, Mapping):
            for key in sorted(item):
                digest.update(json.dumps(key).encode())
                visit(item[key])
        elif isinstance(item, (tuple, list)):
            digest.update(str(len(item)).encode())
            for member in item:
                visit(member)
        else:
            digest.update(json.dumps(item, allow_nan=False).encode())
        digest.update(b"\0")

    visit(value)
    return digest.hexdigest()


@dataclass(frozen=True)
class PublicEvent:
    owner: int
    tick: int
    card_id: int
    x: int
    y: int

    def __post_init__(self):
        if self.owner not in (0, 1) or type(self.tick) is not int or self.tick < 0:
            raise ValueError("invalid public event owner or tick")
        if type(self.card_id) is not int or self.card_id <= 0:
            raise ValueError("invalid public card identity")
        if not 0 <= self.x < 18000 or not 0 <= self.y < 32000:
            raise ValueError("invalid public placement")


class FrozenHistory(Sequence):
    __slots__ = ("_parent", "_items", "_length", "_hashes", "kind", "max_tick")

    def __init__(self, kind):
        if kind not in ("own", "public"):
            raise ValueError("unknown FAIR history kind")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_items", ())
        object.__setattr__(self, "_hashes", ())
        object.__setattr__(self, "_length", 0)
        object.__setattr__(self, "max_tick", -1)

    @classmethod
    def _chunk(cls, kind, parent, items, hashes, max_tick):
        result = cls(kind)
        object.__setattr__(result, "_parent", parent)
        object.__setattr__(result, "_items", items)
        object.__setattr__(result, "_hashes", hashes)
        object.__setattr__(result, "_length", (len(parent) if parent is not None else 0) + len(items))
        object.__setattr__(result, "max_tick", max_tick)
        return result

    def __setattr__(self, name, value):
        raise AttributeError("FAIR history is immutable")

    def __deepcopy__(self, memo):
        return self

    @classmethod
    def freeze(cls, records, kind):
        if isinstance(records, DrawHistory):
            if kind != "own":
                raise ValueError("sampled draws require own history")
            return records
        if isinstance(records, cls):
            if records.kind != kind:
                raise ValueError("FAIR history kind differs")
            return records
        result = cls(kind)
        for record in records:
            result = result.append(record)
        return result

    @property
    def digest(self):
        return self._hashes[-1] if self._hashes else hashlib.sha256(self.kind.encode()).digest()

    def append(self, record):
        if self.kind == "public":
            if type(record) is not PublicEvent:
                raise ValueError("public history contains untyped events")
            maximum = max(self.max_tick, record.tick)
        elif self.kind == "own" and isinstance(record, str):
            maximum = -1
        else:
            raise ValueError("private history requires canonical own-only records")
        digest = hashlib.sha256(self.digest + bytes.fromhex(semantic_digest(record))).digest()
        if len(self._items) == 32:
            return self._chunk(self.kind, self, (record,), (digest,), maximum)
        return self._chunk(self.kind, self._parent, (*self._items, record), (*self._hashes, digest), maximum)

    def __len__(self):
        return self._length

    def __iter__(self):
        chunks, current = [], self
        while current is not None:
            chunks.append(current._items)
            current = current._parent
        for chunk in reversed(chunks):
            yield from chunk

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                return tuple(self)[index]
            chunks, current = [], self
            while current is not None and len(current) > start:
                base = len(current) - len(current._items)
                if stop > base:
                    chunks.append(current._items[max(0, start - base):max(0, stop - base)])
                current = current._parent
            return tuple(record for chunk in reversed(chunks) for record in chunk)
        index = index + len(self) if index < 0 else index
        if not 0 <= index < len(self):
            raise IndexError(index)
        current = self
        while current._parent is not None and index < len(current._parent):
            current = current._parent
        return current._items[index - (len(current) - len(current._items))]

    def extends(self, previous):
        previous = self.freeze(previous, self.kind)
        if len(previous) > len(self):
            return False
        if not previous:
            return True
        if len(self) == len(previous):
            return self.digest == previous.digest
        current = self
        while current._parent is not None and len(current._parent) >= len(previous):
            current = current._parent
        base = len(current) - len(current._items)
        return current._hashes[len(previous) - base - 1] == previous.digest

    def __eq__(self, other):
        if isinstance(other, FrozenHistory):
            return self.kind == other.kind and len(self) == len(other) and self.digest == other.digest
        if isinstance(other, (tuple, list)):
            return tuple(self) == tuple(other)
        return NotImplemented


@dataclass(frozen=True)
class DrawHistory(Sequence):
    initial: tuple[int, ...]
    bindings: tuple[int | None, ...] = (None,) * 4
    phases: tuple = ()

    def __post_init__(self):
        from .config import HOG_DECK
        object.__setattr__(self, "initial", tuple(self.initial))
        object.__setattr__(self, "bindings", tuple(self.bindings))
        if (len(self.initial) != 5 or len(set(self.initial)) != 5
                or not set(self.initial) <= set(HOG_DECK) or len(self.bindings) != 4
                or any(card is not None and card not in HOG_DECK for card in self.bindings)):
            raise ValueError("sampled draw history binding differs")
        phases = tuple(self.phases) or tuple(FrozenHistory("own") for _ in range(5))
        if len(phases) != 5 or any(type(phase) is not FrozenHistory or phase.kind != "own" for phase in phases):
            raise ValueError("sampled draw history phases differ")
        if any(bool(phases[index]) != (card is not None) for index, card in enumerate(self.bindings)):
            raise ValueError("sampled draw observation binding differs")
        object.__setattr__(self, "phases", phases)

    def __deepcopy__(self, memo):
        return self

    def append_decision(self, record, play_count):
        phase = min(play_count, 4)
        if play_count < 0 or any(self.phases[index] for index in range(phase + 1, 5)):
            raise ValueError("sampled draw decision moved backwards")
        entry = dict(record)
        bindings = list(self.bindings)
        if phase < 4:
            next_card = entry.pop("next_card")
            if bindings[phase] not in (None, next_card):
                raise ValueError("sampled draw changed without a public play")
            bindings[phase] = next_card
        phases = list(self.phases)
        phases[phase] = phases[phase].append(json.dumps(entry, sort_keys=True))
        return type(self)(self.initial, tuple(bindings), tuple(phases))

    def rebind(self, initial_queue):
        from .config import HOG_DECK
        if len(initial_queue) != 8 or set(initial_queue) != set(HOG_DECK):
            raise ValueError("sampled initial queue differs")
        bindings = tuple(initial_queue[index + 4] if card is not None else None
                         for index, card in enumerate(self.bindings))
        return type(self)(tuple(initial_queue[:5]), bindings, self.phases)

    def __len__(self):
        return 1 + sum(map(len, self.phases))

    def __iter__(self):
        yield json.dumps(dict(initial_draw=list(self.initial)), sort_keys=True)
        for index, phase in enumerate(self.phases):
            for record in phase:
                if index < 4:
                    entry = json.loads(record)
                    entry["next_card"] = self.bindings[index]
                    yield json.dumps(entry, sort_keys=True)
                else:
                    yield record

    def __getitem__(self, index):
        return tuple(self)[index]

    def to_payload(self):
        return dict(schema="fair-sampled-draw-history.v1", initial=list(self.initial),
                    bindings=list(self.bindings), phases=[list(phase) for phase in self.phases])

    @classmethod
    def from_payload(cls, raw):
        if set(raw) != {"schema", "initial", "bindings", "phases"} or raw["schema"] != "fair-sampled-draw-history.v1":
            raise ValueError("sampled draw history wire fields differ")
        return cls(tuple(raw["initial"]), tuple(raw["bindings"]),
                   tuple(FrozenHistory.freeze(phase, "own") for phase in raw["phases"]))


@dataclass(frozen=True)
class InformationState:
    owner: int
    tick: int
    frame: object
    masks: Mapping[str, torch.Tensor]
    public_history: Sequence[PublicEvent] = ()
    own_history: Sequence[str] = ()
    own_plan: Mapping | None = None

    def __post_init__(self):
        object.__setattr__(self, "public_history", FrozenHistory.freeze(self.public_history, "public"))
        object.__setattr__(self, "own_history", FrozenHistory.freeze(self.own_history, "own"))
        if self.owner not in (0, 1) or type(self.tick) is not int or self.tick < 0:
            raise ValueError("invalid information state")
        if not isinstance(self.frame, tensor_types.UniversalSemanticBatchV4) or self.frame.batch_size != 1:
            raise ValueError("information state requires one FAIR semantic frame")
        if set(self.masks) != {"gate_mask", "card_mask", "cell_masks", "delay_mask"}:
            raise ValueError("FAIR mask fields differ")
        cards = self.frame.own_cards.mask.shape[1]
        expected = {"gate_mask": (1, 2), "card_mask": (1, cards)}
        for name, mask in self.masks.items():
            if mask.dtype != torch.bool or mask.device.type != "cpu":
                raise ValueError("stored FAIR masks require CPU booleans")
            if name in expected and tuple(mask.shape) != expected[name]:
                raise ValueError("FAIR mask shape differs")
            if name not in expected and (mask.ndim != 3 or tuple(mask.shape[:2]) != (1, cards) or mask.shape[2] == 0):
                raise ValueError("conditional mask shape differs")
        if not bool(self.masks["gate_mask"][0, 0]):
            raise ValueError("WAIT must be legal")
        if self.public_history.max_tick > self.tick:
            raise ValueError("public history contains future or untyped events")
        if self.own_plan is not None and (set(self.own_plan) -
                {"card_id", "hand_index", "cell", "delay_ms", "decided_tick", "scheduled_tick"}):
            raise ValueError("own plan fields differ")
        pack(self.frame)

    @property
    def sha256(self):
        return semantic_digest((INFORMATION_DIGEST_SCHEMA, self))

    def legal(self, action):
        if action == "wait":
            return True
        parts = action.split(":")
        if len(parts) != 4 or parts[0] != "play":
            return False
        try:
            row, cell, delay = map(int, parts[1:])
            if action != f"play:{row}:{cell}:{delay}":
                return False
            masks = self.masks
            return (0 <= row < masks["card_mask"].shape[1]
                    and 0 <= cell < masks["cell_masks"].shape[2]
                    and 0 <= delay < masks["delay_mask"].shape[2]
                    and bool(masks["gate_mask"][0, 1] and masks["card_mask"][0, row]
                             and masks["cell_masks"][0, row, cell] and masks["delay_mask"][0, row, delay]))
        except (ValueError, IndexError):
            return False

    def to_payload(self):
        return dict(owner=self.owner, tick=self.tick, frame=pack(self.frame), masks=pack(self.masks),
                    public_history=[asdict(event) for event in self.public_history],
                    own_history=(self.own_history.to_payload() if isinstance(self.own_history, DrawHistory)
                                 else list(self.own_history)), own_plan=pack(self.own_plan))

    @classmethod
    def from_payload(cls, raw):
        if set(raw) != {field.name for field in fields(cls)}:
            raise ValueError("information wire fields differ")
        frame = unpack(raw["frame"])
        masks = unpack(raw["masks"])
        own_plan = unpack(raw["own_plan"])
        if not isinstance(masks, Mapping):
            raise ValueError("FAIR wire masks must be a mapping")
        return cls(raw["owner"], raw["tick"], frame, masks,
                   tuple(PublicEvent(**event) for event in raw["public_history"]),
                   (DrawHistory.from_payload(raw["own_history"]) if isinstance(raw["own_history"], dict)
                    else tuple(raw["own_history"])), own_plan)


@dataclass(frozen=True)
class SearchDecision:
    index: int
    information: InformationState
    visits: Mapping[str, int]
    selected: str
    model_sha256: str
    config_sha256: str
    runtime_sha256: str
    result: int
    schema: str = "fair-search-decision.v1"

    def __post_init__(self):
        if self.schema != "fair-search-decision.v1" or type(self.index) is not int or self.index < 0:
            raise ValueError("decision contract differs")
        if isinstance(self.information.own_history, DrawHistory):
            raise ValueError("sampled opponent history is not an actual training decision")
        if not self.visits or len(self.visits) > 32 or any(type(count) is not int or count < 0 for count in self.visits.values()):
            raise ValueError("invalid sparse visits")
        if sum(self.visits.values()) != 32 or any(not self.information.legal(action) for action in self.visits):
            raise ValueError("search budget or action legality differs")
        if self.visits.get(self.selected, 0) < 1 or type(self.result) is not int or self.result not in (-1, 0, 1):
            raise ValueError("selected action or terminal target differs")
        for identity in (self.model_sha256, self.config_sha256, self.runtime_sha256):
            require_sha(identity)

    def to_payload(self):
        return {**{field.name: getattr(self, field.name) for field in fields(self)},
                "information": self.information.to_payload(), "visits": dict(self.visits)}

    @classmethod
    def from_payload(cls, raw):
        if set(raw) != {field.name for field in fields(cls)}:
            raise ValueError("decision wire fields differ")
        return cls(**{**raw, "information": InformationState.from_payload(raw["information"])})


@dataclass(frozen=True)
class SearchEpisode:
    episode_id: str
    generation: int
    decisions: tuple[SearchDecision, ...]
    winner: int | None
    terminal_tick: int
    reason: str
    current_sha256: str
    owner_sha256: tuple[str, str]
    attempt: int = 0
    schema: str = "fair-search-episode.v2"

    def __post_init__(self):
        if self.schema != "fair-search-episode.v2" or not self.episode_id or not self.reason:
            raise ValueError("episode contract differs")
        if type(self.generation) is not int or self.generation < 1 or type(self.attempt) is not int or self.attempt < 0:
            raise ValueError("invalid generation or attempt")
        if self.winner not in (None, 0, 1) or type(self.terminal_tick) is not int or self.terminal_tick < 1:
            raise ValueError("episode needs a native terminal result")
        require_sha(self.current_sha256)
        if len(self.owner_sha256) != 2:
            raise ValueError("episode needs two model identities")
        for identity in self.owner_sha256:
            require_sha(identity)
        if not self.decisions or len({row.index for row in self.decisions}) != len(self.decisions):
            raise ValueError("episode decisions are empty or duplicated")
        for row in self.decisions:
            expected = 0 if self.winner is None else (1 if self.winner == row.information.owner else -1)
            if row.result != expected or row.information.tick > self.terminal_tick:
                raise ValueError("terminal target or tick mismatch")
            if row.model_sha256 != self.current_sha256 or self.owner_sha256[row.information.owner] != row.model_sha256:
                raise ValueError("only current-policy rows may train")

    def to_payload(self):
        return {**{field.name: getattr(self, field.name) for field in fields(self)},
                "owner_sha256": list(self.owner_sha256),
                "decisions": [row.to_payload() for row in self.decisions]}

    @classmethod
    def from_payload(cls, raw):
        if set(raw) != {field.name for field in fields(cls)}:
            raise ValueError("episode wire fields differ; PPO data is not accepted")
        return cls(**{**raw, "owner_sha256": tuple(raw["owner_sha256"]),
                      "decisions": tuple(SearchDecision.from_payload(row) for row in raw["decisions"])})
