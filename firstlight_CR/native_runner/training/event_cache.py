"""Event-boundary observation cache for the event-driven policy.

The fixed-step V4 cache stores one frame per five-tick decision and its action
label shares the legacy five-bin delay vocabulary.  Event decisions instead
occur at arbitrary public deployment ticks and carry a sixty-one bin delay
label, so they need their own container.  The storage mechanics (safetensors
payload, Zstandard framing, dynamic-capacity padding) intentionally mirror
``training/v4/cache.py`` so both caches stay comparable and auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import math
from typing import Iterator, Mapping, Sequence

import torch

from .event_config import EventPolicyConfigV1, EVENT_POLICY_VERSION
from .v4.cache import (
    _CONTAINER_HEADER,
    _CONTAINER_MAGIC,
    _atomic_write,
    _atomic_write_json,
    _canonical_json,
    _flatten_record,
    _pad_dynamic_observation,
    _restore_dynamic_observation,
    _restore_record,
    _sha256,
)
from .v4.tensors import UniversalSemanticBatchV4


EVENT_CACHE_SHARD_SCHEMA = "event-observation-cache-shard.v1"
EVENT_CACHE_SUMMARY_SCHEMA = "event-observation-cache-summary.v1"
EVENT_RL_SHARD_SCHEMA = "event-rl-shard.v1"
EVENT_RL_SUMMARY_SCHEMA = "event-rl-summary.v1"
EVENT_CACHE_CONTRACT_VERSION = "event-observation-cache-contract.v2"
EVENT_ACTION_CELL_FRAME = "actor_relative.v1"
EVENT_SUPERVISED_INPUT_SEMANTICS = "owner-neutral.v1"


@dataclass(frozen=True, slots=True)
class EventObservationRecordV1:
    """One event observation and label; v2 contracts store model-relative cells.

    Owner and hand slot remain native audit/candidate identities. Legacy records
    remain readable, so supervised consumers must validate the shard contract.
    """

    replay_tag: str
    owner: int
    observation_tick: int
    trigger_event_id: str
    trigger_owner: int | None
    trigger_card_id: int | None
    wait: bool
    card_id: int | None
    hand_slot: int | None
    target_cell: tuple[int, int] | None
    delay_ms: int
    delay_bin: int
    target_tick: int | None
    next_trigger_tick: int | None
    terminal: bool
    candidate_index: int
    candidate_uid: int
    legal_at_trigger: bool
    card_in_hand: bool

    def __post_init__(self) -> None:
        if self.target_cell is not None and not isinstance(self.target_cell, tuple):
            object.__setattr__(self, "target_cell", tuple(self.target_cell))
        if self.owner not in (0, 1):
            raise ValueError("event observation owner must be 0 or 1")
        if self.trigger_owner not in (None, 0, 1):
            raise ValueError("event trigger owner must be 0, 1, or null")
        if self.observation_tick < 0:
            raise ValueError("event observation tick must be non-negative")
        if self.wait:
            if self.card_id is not None or self.target_cell is not None:
                raise ValueError("WAIT event label cannot carry a card or target")
        else:
            if self.card_id is None or self.target_cell is None:
                raise ValueError("ACT event label needs a card and target")
            x, y = self.target_cell
            if not 0 <= x < 18 or not 0 <= y < 32:
                raise ValueError("event label target cell is outside the 18x32 grid")
        if self.delay_ms < 0 or self.delay_ms % 100 != 0:
            raise ValueError("event delay label must be a non-negative 100ms multiple")
        if self.delay_bin < 0:
            raise ValueError("event delay bin must be non-negative")
        if self.legal_at_trigger and (self.wait or self.candidate_index < 0):
            raise ValueError("a legal event ACT label needs a candidate index")

    @property
    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["target_cell"] = list(self.target_cell) if self.target_cell is not None else None
        return value


def canonicalize_event_observation(
    observation: UniversalSemanticBatchV4,
) -> UniversalSemanticBatchV4:
    """Use precisely the persisted feature values before behavior sampling.

    Delegate to storage conversion, including integer packing and the FP32
    candidate elixir/cost exceptions; a blanket half() is not equivalent.
    Persist this returned frame, never rewrite recorded probabilities.
    """
    return observation.to_storage("cpu", float_dtype=torch.float16).to_model_input(
        "cpu", float_dtype=torch.float32
    )


def _pack_observations(
    observations: Sequence[UniversalSemanticBatchV4],
    storage_float_dtype: torch.dtype | None,
) -> tuple[dict[str, torch.Tensor], dict[str, int]]:
    """Pack per-record observations into one flattened tensor dict.

    Dynamic-capacity groups (effects, relation edges, candidates) are padded to
    the shard maximum so every frame shares one batch; the per-record counts
    ride along so a loader can slice a frame back down to its own capacity.
    """

    if not observations:
        raise ValueError("event shard needs at least one observation")
    active_effect_counts: list[int] = []
    relation_edge_counts: list[int] = []
    candidate_counts: list[int] = []
    for observation in observations:
        if observation.batch_size != 1:
            raise ValueError("event shard records must have batch size one")
        active_effect_counts.append(int(observation.active_effects.mask.shape[1]))
        relation_edge_counts.append(int(observation.relation_edges.mask.shape[1]))
        candidate_counts.append(int(observation.candidates.mask.shape[1]))
    capacities = {
        "active_effects": max(active_effect_counts),
        "relation_edges": max(relation_edge_counts),
        "candidates": max(candidate_counts),
    }

    from .v4.tensors import concatenate_tensor_records

    packed = concatenate_tensor_records(
        tuple(
            _pad_dynamic_observation(
                observation,
                active_effect_count=capacities["active_effects"],
                relation_edge_count=capacities["relation_edges"],
                candidate_count=capacities["candidates"],
            )
            for observation in observations
        )
    )
    if not isinstance(packed, UniversalSemanticBatchV4):
        raise TypeError("event shard observations have an unexpected record type")
    if storage_float_dtype is not None:
        packed = packed.to_storage("cpu", float_dtype=storage_float_dtype)

    tensors: dict[str, torch.Tensor] = {}
    _flatten_record(packed, prefix="observations", destination=tensors)
    tensors["active_effect_counts"] = torch.tensor(active_effect_counts, dtype=torch.int32)
    tensors["relation_edge_counts"] = torch.tensor(relation_edge_counts, dtype=torch.int32)
    tensors["candidate_counts"] = torch.tensor(candidate_counts, dtype=torch.int32)
    for name, tensor in tensors.items():
        if tensor.is_floating_point() and not bool(torch.isfinite(tensor).all()):
            raise ValueError(f"event shard tensor {name} contains non-finite values")
    return tensors, capacities


def _store_container(
    data_path: Path,
    tensors: dict[str, torch.Tensor],
    manifest: dict[str, object],
    *,
    compression_level: int,
) -> dict[str, object]:
    """Compress one container and store it atomically with its checksums."""

    import zstandard
    from safetensors.torch import save as save_safetensors

    manifest_payload = _canonical_json(manifest)
    tensor_payload = save_safetensors(tensors)
    container = _CONTAINER_HEADER.pack(_CONTAINER_MAGIC, len(manifest_payload)) + manifest_payload + tensor_payload
    compressed = zstandard.ZstdCompressor(level=compression_level, threads=1).compress(container)
    _atomic_write(data_path, compressed)
    return {
        "data_file": data_path.name,
        "compressed_sha256": _sha256(compressed),
        "container_sha256": _sha256(container),
        "compressed_bytes": len(compressed),
        "container_bytes": len(container),
        "compression_ratio": round(len(container) / max(1, len(compressed)), 6),
    }


def _load_container(
    summary_file: Path,
    summary_schema: str,
    *,
    verify: bool,
    source=None,
) -> tuple[dict[str, object], dict[str, torch.Tensor], Path]:
    """Verify, decompress and unpack one container's manifest and tensors."""

    try:
        import zstandard
        from safetensors.torch import load as load_safetensors
    except ImportError as error:
        raise RuntimeError("event caching requires zstandard and safetensors") from error

    summary = json.loads(summary_file.read_text(encoding="utf-8")) if source is None else source[0]
    if not isinstance(summary, dict) or summary.get("schema") != summary_schema:
        raise ValueError("unsupported event cache summary")
    data_name = summary.get("data_file")
    if not isinstance(data_name, str) or Path(data_name).name != data_name:
        raise ValueError("event cache summary data file must be a local basename")
    data_path = summary_file.parent / data_name
    compressed = data_path.read_bytes() if source is None else source[1]
    if verify and _sha256(compressed) != summary.get("compressed_sha256"):
        raise ValueError("event cache shard checksum mismatch")
    container = zstandard.ZstdDecompressor().decompress(compressed)
    if verify and _sha256(container) != summary.get("container_sha256"):
        raise ValueError("event cache container checksum mismatch")
    if len(container) < _CONTAINER_HEADER.size:
        raise ValueError("event cache container is truncated")
    magic, manifest_length = _CONTAINER_HEADER.unpack_from(container)
    if magic != _CONTAINER_MAGIC or manifest_length <= 0:
        raise ValueError("event cache container header is invalid")
    manifest_end = _CONTAINER_HEADER.size + manifest_length
    manifest = json.loads(container[_CONTAINER_HEADER.size : manifest_end])
    if not isinstance(manifest, dict):
        raise ValueError("event cache shard manifest is invalid")
    return manifest, load_safetensors(container[manifest_end:]), data_path


def write_event_cache_shard(
    destination: str | Path,
    records: Sequence[EventObservationRecordV1],
    observations: Sequence[UniversalSemanticBatchV4],
    *,
    shard_id: str,
    contract: Mapping[str, object],
    compression_level: int = -5,
    storage_float_dtype: torch.dtype | None = torch.float16,
) -> dict[str, object]:
    """Pack event observations into one checksummed Zstandard shard."""

    try:
        import zstandard  # noqa: F401
    except ImportError as error:
        raise RuntimeError("event caching requires safetensors and zstandard") from error
    if not -5 <= compression_level <= 22:
        raise ValueError("Zstandard compression level must be in -5..22")
    if len(records) != len(observations) or not records:
        raise ValueError("event cache shard needs one observation per record")

    tensors, capacities = _pack_observations(observations, storage_float_dtype)

    manifest = {
        "schema": EVENT_CACHE_SHARD_SCHEMA,
        "shard_id": str(shard_id),
        "contract": dict(contract),
        "records": [record.as_dict for record in records],
        "frames": len(records),
        "dynamic_capacities": dict(capacities),
        "tensor_count": len(tensors),
    }

    data_path = Path(destination)
    if not data_path.name.endswith(".crev.zst"):
        raise ValueError("event cache shard destination must end with .crev.zst")
    summary_path = data_path.with_name(data_path.name[: -len(".crev.zst")] + ".json")
    summary: dict[str, object] = {
        "schema": EVENT_CACHE_SUMMARY_SCHEMA,
        "shard_schema": EVENT_CACHE_SHARD_SCHEMA,
        "shard_id": str(shard_id),
        **_store_container(data_path, tensors, manifest, compression_level=compression_level),
        "replays": len({record.replay_tag for record in records}),
        "frames": len(records),
        "act_frames": sum(not record.wait for record in records),
        "wait_frames": sum(record.wait for record in records),
        "legal_act_frames": sum(record.legal_at_trigger for record in records),
        "frames_in_hand": sum(record.card_in_hand for record in records),
    }
    _atomic_write_json(summary_path, summary)
    return summary


@dataclass(slots=True)
class EventCacheShardV1:
    """One decompressed event shard with per-record observation access."""

    path: Path
    manifest: dict[str, object]
    summary: dict[str, object]
    records: tuple[EventObservationRecordV1, ...]
    _observations: UniversalSemanticBatchV4 = field(repr=False)
    _active_effect_counts: torch.Tensor
    _relation_edge_counts: torch.Tensor
    _candidate_counts: torch.Tensor

    @property
    def frames(self) -> int:
        return len(self.records)

    def observations_batch(self) -> UniversalSemanticBatchV4:
        """All shard frames as one batch; capacity is uniform within a shard."""

        return self._observations

    def observation(self, index: int) -> UniversalSemanticBatchV4:
        record = self.records[index]
        return _restore_dynamic_observation(
            self._observations.narrow_batch(index, 1),
            active_effect_count=int(self._active_effect_counts[index]),
            relation_edge_count=int(self._relation_edge_counts[index]),
            candidate_count=int(self._candidate_counts[index]),
        )

    def observations(self) -> Iterator[UniversalSemanticBatchV4]:
        for index in range(self.frames):
            yield self.observation(index)


def load_event_cache_shard(summary_path: str | Path, *, verify: bool = False) -> EventCacheShardV1:
    summary_file = Path(summary_path)
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    manifest, tensors, data_path = _load_container(summary_file, EVENT_CACHE_SUMMARY_SCHEMA, verify=verify)
    if manifest.get("schema") != EVENT_CACHE_SHARD_SCHEMA:
        raise ValueError("unsupported event cache shard")
    records = tuple(EventObservationRecordV1(**item) for item in manifest["records"])
    framed = _restore_record(UniversalSemanticBatchV4, prefix="observations", tensors=tensors)
    if framed.batch_size != len(records):
        raise ValueError("event cache observation frame count disagrees with its manifest")
    return EventCacheShardV1(
        path=data_path,
        manifest=manifest,
        summary=summary,
        records=records,
        _observations=framed,
        _active_effect_counts=tensors["active_effect_counts"],
        _relation_edge_counts=tensors["relation_edge_counts"],
        _candidate_counts=tensors["candidate_counts"],
    )


def validate_event_supervised_contract(contract: Mapping[str, object] | None) -> None:
    """Reject ambiguous/native labels before supervised training or evaluation.

    Pass each shard manifest's contract, not only the cache index's contract.
    Low-level readers intentionally permit legacy inspection without upgrading
    metadata. Re-capture or an independently verified migration is required to
    assert v2 semantics; there is no implicit legacy opt-in here.

    Input semantics require the event model to neutralize the absolute owner
    feature. Stored observation tensors retain their original audit features.
    """
    expected = {
        "version": EVENT_CACHE_CONTRACT_VERSION,
        "action_cell_frame": EVENT_ACTION_CELL_FRAME,
        "input_semantics": EVENT_SUPERVISED_INPUT_SEMANTICS,
    }
    if not isinstance(contract, Mapping):
        raise ValueError("supervised event cache contract is missing or invalid")
    for field_name, required in expected.items():
        if contract.get(field_name) != required:
            raise ValueError(
                f"unsupported supervised event cache contract: {field_name} must be "
                f"{required!r}, got {contract.get(field_name)!r}; legacy/native labels "
                "require re-capture or an explicitly verified migration"
            )


def default_event_contract(config: EventPolicyConfigV1 | None = None) -> dict[str, object]:
    actual = config or EventPolicyConfigV1()
    return {
        "version": EVENT_CACHE_CONTRACT_VERSION,
        "action_cell_frame": EVENT_ACTION_CELL_FRAME,
        "input_semantics": EVENT_SUPERVISED_INPUT_SEMANTICS,
        "policy_version": EVENT_POLICY_VERSION,
        "delay_offset_ms": list(actual.delay_offset_ms),
        "max_wait_ticks": actual.max_wait_ticks,
        "storage_float_dtype": "float16",
        "pipeline_phase": "event-observation-capture-only",
    }


def write_event_cache_index(
    path: str | Path, summaries: Sequence[Mapping[str, object]], *, contract: Mapping[str, object]
) -> dict[str, object]:
    """Write a shard index whose self-hash excludes the hash field itself."""

    entries = [dict(summary) for summary in summaries]
    body: dict[str, object] = {
        "schema": "event-observation-cache-index.v1",
        "contract": dict(contract),
        "shards": entries,
        "shard_count": len(entries),
        "frames": sum(int(entry.get("frames", 0)) for entry in entries),
        "act_frames": sum(int(entry.get("act_frames", 0)) for entry in entries),
        "wait_frames": sum(int(entry.get("wait_frames", 0)) for entry in entries),
        "legal_act_frames": sum(int(entry.get("legal_act_frames", 0)) for entry in entries),
        "replays": sum(int(entry.get("replays", 0)) for entry in entries),
    }
    body["index_sha256"] = _sha256(_canonical_json(body))
    _atomic_write_json(Path(path), body)
    return body


@dataclass(frozen=True, slots=True)
class EventRlRecordV1:
    """One decided step from a rollout, with everything PPO needs to re-score it.

    Unlike :class:`EventObservationRecordV1`, which pairs an observation with an
    expert label, this records the *behaviour*: the action came from the policy
    that played the match, so the stored log-prob belongs to that policy at the
    stored temperatures. An importance ratio only means something while those
    agree, which is why the four temperatures travel with every row.
    """

    match_id: str
    owner: int
    observation_tick: int
    decision_ticks: int
    act: bool
    card_row: int
    cell: int
    delay_bin: int
    log_prob: float
    gate_log_prob: float
    card_log_prob: float
    cell_log_prob: float
    delay_log_prob: float
    gate_entropy: float
    card_entropy: float
    cell_entropy: float
    delay_entropy: float
    temperatures: tuple[float, float, float, float]
    reward: float
    done: bool
    truncated: bool
    policy_version: str
    deck: tuple[int, ...]
    opponent_deck: tuple[int, ...]
    # Optional only for reading legacy shards; identity-bound training requires both.
    behavior_checkpoint_sha256: str | None = None
    behavior_model_contract_hash: str | None = None

    # Missing in historical shards: these remain readable for auditing, but
    # cannot be used by the execution-confirmed learner.
    execution_status: str = "legacy_unknown"
    action_id: str | None = None
    exclusion_reason: str | None = None
    # Missing only in historical terminal-only shards. Penalty is a positive cost.
    base_reward: float | None = None
    overflow_penalty: float = 0.0
    tower_state: tuple[tuple[int, int, str, float, float], ...] | None = None
    tower_shaping: float = 0.0
    ability_candidate: int = -1
    ability_source_entity: int | None = None
    ability_vocab_id: int | None = None
    ability_log_prob: float = 0.0
    ability_entropy: float = 0.0

    HEAD_TERMS = ("card_log_prob", "cell_log_prob", "delay_log_prob")
    HEAD_ENTROPIES = ("card_entropy", "cell_entropy", "delay_entropy")

    def __post_init__(self) -> None:
        if not math.isfinite(self.tower_shaping):
            raise ValueError("nonfinite tower shaping reward")
        if self.tower_state is not None:
            from .event_tower_rewards import validate_tower_state
            object.__setattr__(self, "tower_state", tuple(tuple(t) for t in self.tower_state))
            validate_tower_state(self.tower_state)
        if not math.isfinite(self.overflow_penalty) or self.overflow_penalty < 0:
            raise ValueError("invalid overflow penalty")
        if self.base_reward is None:
            if self.overflow_penalty or self.tower_shaping:
                raise ValueError("overflow penalty requires explicit reward components")
        elif (not math.isfinite(self.base_reward) or
              not math.isclose(self.reward, self.base_reward-self.overflow_penalty+self.tower_shaping, abs_tol=1e-9)):
            raise ValueError("event reward components do not conserve total reward")
        if self.execution_status not in {"legacy_unknown", "wait", "executed", "cancelled", "unconfirmed"}:
            raise ValueError("invalid event RL execution status")
        if self.execution_status != "legacy_unknown":
            if (self.execution_status == "wait") == self.act:
                raise ValueError("execution status disagrees with ACT/WAIT")
            if self.execution_status in {"executed", "unconfirmed"} and not self.action_id:
                raise ValueError("execution status requires an action_id")
            if self.execution_status in {"cancelled", "unconfirmed"} and not self.exclusion_reason:
                raise ValueError("excluded decision requires a reason")
            if self.execution_status in {"wait", "executed"} and self.exclusion_reason is not None:
                raise ValueError("trainable decision cannot have an exclusion reason")
        if self.owner not in (0, 1):
            raise ValueError("event RL record owner must be 0 or 1")
        if self.observation_tick < 0 or self.decision_ticks < 0:
            raise ValueError("event RL record ticks must be non-negative")
        if self.done and self.truncated:
            raise ValueError("an event RL step cannot be both done and truncated")
        if not isinstance(self.temperatures, tuple):
            object.__setattr__(self, "temperatures", tuple(self.temperatures))
        if len(self.temperatures) != 4 or any(
            not math.isfinite(value) or value <= 0.0 for value in self.temperatures
        ):
            raise ValueError("event RL temperatures must be four finite positive values")
        scalars = (
            self.log_prob,
            self.gate_log_prob,
            self.card_log_prob,
            self.cell_log_prob,
            self.delay_log_prob,
            self.gate_entropy,
            self.card_entropy,
            self.cell_entropy,
            self.delay_entropy,
            self.reward,
        )
        if any(not math.isfinite(value) for value in scalars):
            raise ValueError("event RL record holds a non-finite value")
        if (type(self.ability_candidate) is not int or self.ability_candidate < -1
                or not math.isfinite(self.ability_log_prob) or not math.isfinite(self.ability_entropy)):
            raise ValueError("invalid event ability record")
        if self.ability_candidate >= 0:
            if (not self.act or (self.card_row, self.cell) != (-1, -1) or self.delay_bin < -1
                    or type(self.ability_source_entity) is not int or self.ability_source_entity < 0
                    or type(self.ability_vocab_id) is not int or self.ability_vocab_id < 1
                    or any(getattr(self, name) != 0 for name in ('card_log_prob', 'cell_log_prob', 'card_entropy', 'cell_entropy'))
                    or (self.delay_bin == -1 and (self.delay_log_prob != 0 or self.delay_entropy != 0))):
                raise ValueError("ability event requires an exact source and no card/cell terms")
        elif (self.ability_source_entity is not None or self.ability_vocab_id is not None
              or self.ability_log_prob != 0 or self.ability_entropy != 0):
            raise ValueError("non-ability event carries ability fields")
        elif self.act:
            if min(self.card_row, self.cell, self.delay_bin) < 0:
                raise ValueError("an ACT event RL record needs a card, cell and delay")
        else:
            if max(self.card_row, self.cell, self.delay_bin) >= 0:
                raise ValueError("a WAIT event RL record must not carry an action")
            if any(abs(getattr(self, name)) > 0.0 for name in self.HEAD_TERMS):
                raise ValueError("a WAIT event RL record carries no card, cell or delay terms")
            if any(abs(getattr(self, name)) > 0.0 for name in self.HEAD_ENTROPIES):
                raise ValueError("a WAIT event RL record carries no card, cell or delay entropy")
        for deck in (self.deck, self.opponent_deck):
            if len(deck) != 8 or len(set(deck)) != 8:
                raise ValueError("event RL record decks must hold eight unique cards")

    @property
    def trainable(self) -> bool:
        return self.execution_status in {"wait", "executed"}

    @property
    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["temperatures"] = list(self.temperatures)
        value["deck"] = list(self.deck)
        value["opponent_deck"] = list(self.opponent_deck)
        return value


def _per_row_mask(mask: torch.Tensor) -> torch.Tensor:
    """Accept a mask with or without its singleton batch dimension."""

    if mask.ndim and int(mask.shape[0]) == 1:
        return mask[0]
    return mask


def write_event_rl_shard(
    destination: str | Path,
    records: Sequence[EventRlRecordV1],
    observations: Sequence[UniversalSemanticBatchV4],
    *,
    shard_id: str,
    contract: Mapping[str, object],
    masks: Sequence[tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]] | None = None,
    compression_level: int = -5,
    storage_float_dtype: torch.dtype | None = torch.float16,
) -> dict[str, object]:
    """Pack policy-behaviour rows and their observations into one shard.

    ``masks`` carries each row's ``(card_mask, cell_mask)``. Legality comes from
    the engine and is not derivable from the observation tensors, so a re-scoring
    pass could not reconstruct it — and scoring a stored action under a wider
    mask would silently change its probability.
    """

    try:
        import zstandard  # noqa: F401
    except ImportError as error:
        raise RuntimeError("event caching requires safetensors and zstandard") from error
    if not -5 <= compression_level <= 22:
        raise ValueError("Zstandard compression level must be in -5..22")
    if len(records) != len(observations) or not records:
        raise ValueError("event RL shard needs one observation per record")

    tensors, capacities = _pack_observations(observations, storage_float_dtype)
    if masks is not None:
        if len(masks) != len(records):
            raise ValueError("event RL shard needs one mask pair per record")
        card_masks = torch.stack([_per_row_mask(pair[0]).to(torch.bool).cpu() for pair in masks])
        cell_masks = torch.stack([_per_row_mask(pair[1]).to(torch.bool).cpu() for pair in masks])
        delay_masks = torch.stack([_per_row_mask(pair[2]).to(torch.bool).cpu() for pair in masks])
        gate_masks = torch.stack([_per_row_mask(pair[3]).to(torch.bool).cpu() for pair in masks])
        if card_masks.ndim != 2 or cell_masks.ndim != 3 or cell_masks.shape[0] != card_masks.shape[0]:
            raise ValueError("event RL masks must be [T, cards] and [T, cards, cells]")
        if delay_masks.ndim != 3 or delay_masks.shape[:2] != card_masks.shape:
            raise ValueError("event RL delay masks must be [T, cards, delay bins]")
        tensors["rl_card_mask"] = card_masks
        tensors["rl_cell_mask"] = cell_masks
        tensors["rl_delay_mask"] = delay_masks
        if gate_masks.ndim != 2 or gate_masks.shape[1] != 2:
            raise ValueError("event RL gate masks must be [T, 2] over (WAIT, ACT)")
        tensors["rl_gate_mask"] = gate_masks
    manifest = {
        "schema": EVENT_RL_SHARD_SCHEMA,
        "shard_id": str(shard_id),
        "contract": dict(contract),
        "records": [record.as_dict for record in records],
        "frames": len(records),
        "dynamic_capacities": dict(capacities),
        "tensor_count": len(tensors),
    }

    data_path = Path(destination)
    if not data_path.name.endswith(".crev.zst"):
        raise ValueError("event RL shard destination must end with .crev.zst")
    summary_path = data_path.with_name(data_path.name[: -len(".crev.zst")] + ".json")
    summary: dict[str, object] = {
        "schema": EVENT_RL_SUMMARY_SCHEMA,
        "shard_schema": EVENT_RL_SHARD_SCHEMA,
        "shard_id": str(shard_id),
        **_store_container(data_path, tensors, manifest, compression_level=compression_level),
        "frames": len(records),
        "act_frames": sum(record.act for record in records),
        "trainable_frames": sum(record.trainable for record in records),
        "excluded_frames": sum(not record.trainable for record in records),
        "execution_status_counts": {
            status: sum(record.execution_status == status for record in records)
            for status in sorted({record.execution_status for record in records})
        },
        "wait_frames": sum(not record.act for record in records),
        "rewarded_frames": sum(record.reward != 0.0 for record in records),
        "terminal_frames": sum(record.done for record in records),
        "truncated_frames": sum(record.truncated for record in records),
        "matches": len({record.match_id for record in records}),
        "policies": sorted({record.policy_version for record in records}),
        "has_masks": masks is not None,
    }
    from .event_rewards import reward_components
    summary["reward_components"] = reward_components(records, contract.get("reward_config"))
    summary["reward_config"] = contract.get("reward_config")
    _atomic_write_json(summary_path, summary)
    return summary


@dataclass(slots=True)
class EventRlShardV1:
    """One decompressed rollout shard with per-row observation access."""

    path: Path
    manifest: dict[str, object]
    summary: dict[str, object]
    records: tuple[EventRlRecordV1, ...]
    _observations: UniversalSemanticBatchV4 = field(repr=False)
    _active_effect_counts: torch.Tensor
    _relation_edge_counts: torch.Tensor
    _candidate_counts: torch.Tensor
    card_masks: torch.Tensor | None = None
    cell_masks: torch.Tensor | None = None
    delay_masks: torch.Tensor | None = None
    gate_masks: torch.Tensor | None = None

    @property
    def frames(self) -> int:
        return len(self.records)

    def observations_batch(self) -> UniversalSemanticBatchV4:
        return self._observations

    def observation(self, index: int) -> UniversalSemanticBatchV4:
        return _restore_dynamic_observation(
            self._observations.narrow_batch(index, 1),
            active_effect_count=int(self._active_effect_counts[index]),
            relation_edge_count=int(self._relation_edge_counts[index]),
            candidate_count=int(self._candidate_counts[index]),
        )

    def observations(self) -> Iterator[UniversalSemanticBatchV4]:
        for index in range(self.frames):
            yield self.observation(index)


def load_event_rl_shard(summary_path: str | Path, *, verify: bool = False, _source=None) -> EventRlShardV1:
    summary_file = Path(summary_path)
    summary = json.loads(summary_file.read_text(encoding="utf-8")) if _source is None else _source[0]
    manifest, tensors, data_path = _load_container(summary_file, EVENT_RL_SUMMARY_SCHEMA, verify=verify, source=_source)
    if manifest.get("schema") != EVENT_RL_SHARD_SCHEMA:
        raise ValueError("unsupported event RL shard")
    records = tuple(EventRlRecordV1(**item) for item in manifest["records"])
    framed = _restore_record(UniversalSemanticBatchV4, prefix="observations", tensors=tensors)
    if framed.batch_size != len(records):
        raise ValueError("event RL observation frame count disagrees with its manifest")
    return EventRlShardV1(
        path=data_path,
        manifest=manifest,
        summary=summary,
        records=records,
        _observations=framed,
        _active_effect_counts=tensors["active_effect_counts"],
        _relation_edge_counts=tensors["relation_edge_counts"],
        _candidate_counts=tensors["candidate_counts"],
        card_masks=tensors.get("rl_card_mask"),
        cell_masks=tensors.get("rl_cell_mask"),
        delay_masks=tensors.get("rl_delay_mask"),
        gate_masks=tensors.get("rl_gate_mask"),
    )
