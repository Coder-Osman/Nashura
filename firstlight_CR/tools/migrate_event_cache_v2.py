#!/usr/bin/env python3
"""Offline, explicit cell-frame v1 -> actor-relative v2 cache migration.

python tools/migrate_event_cache_v2.py --source OLD --output NEW \
    --confirmed-cell-frame native --evidence-file PROOF.json --workers 4

The confirmation asserts EXTERNALLY established label-frame provenance; this
program cannot infer it. Observations must already be perspective normalized.
Each source *.crev.zst must have its sibling JSON summary. All paths, including
duplicate-content shards, are retained under train/ or val/. Replay assignment
is int(sha256(replay_tag UTF-8), 16) / 2**256 < val_fraction. Shards spanning
both partitions are rejected (re-shard them separately before migration).

Zstd must be decoded/re-encoded because metadata and tensors share a frame.
Safetensors bytes are copied verbatim: no torch tensor decoding/reconstruction.
Memory is bounded by workers times shard size. Restart the identical command to
resume; completed source/output checksums are reverified. Nothing is deleted.
An interrupted unjournaled write is accepted only if its bytes match exactly.
Indexes are published only after all shards pass. Do not consume partial runs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.perspective import PerspectiveTransformV1
from native_runner.training.event_cache import (
    EVENT_CACHE_SHARD_SCHEMA, EVENT_CACHE_SUMMARY_SCHEMA,
    EventObservationRecordV1, default_event_contract,
    validate_event_supervised_contract, write_event_cache_index,
)
from native_runner.training.v4.cache import (
    _CONTAINER_HEADER, _CONTAINER_MAGIC, _canonical_json, _sha256,
)

LEGACY_VERSION = "event-observation-cache-contract.v1"
TRANSFORM = "PerspectiveTransformV1(actor_owner=owner)._grid; owner0 identity; owner1 flip180"


def replay_partition(replay_tag: str, val_fraction: float = 0.05) -> str:
    if not isinstance(replay_tag, str) or not replay_tag:
        raise ValueError("every record must have a nonempty replay_tag")
    return "val" if int.from_bytes(hashlib.sha256(replay_tag.encode()).digest(), "big") < int(val_fraction * (1 << 256)) else "train"


def _publish(path: Path, payload: bytes) -> None:
    """Exclusive publication; never replace an existing file, even on resume."""
    if path.is_symlink():
        raise ValueError(f"output symlink is forbidden: {path}")
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"existing output differs; refusing overwrite: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".migration-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def _verified_source(source: Path, relative: str):
    summary_path = source / relative
    raw_summary = summary_path.read_bytes()
    summary = json.loads(raw_summary)
    if summary.get("schema") != EVENT_CACHE_SUMMARY_SCHEMA:
        raise ValueError(f"unsupported summary schema: {summary_path}")
    name = summary.get("data_file")
    expected = summary_path.name[:-5] + ".crev.zst"
    if name != expected:
        raise ValueError(f"summary must reference its sibling shard {expected}: {summary_path}")
    compressed = (summary_path.parent / name).read_bytes()
    if _sha256(compressed) != summary.get("compressed_sha256"):
        raise ValueError(f"source compressed checksum mismatch: {summary_path}")
    return raw_summary, summary, compressed


def _migrate_one(job):
    source_root, output_root, relative, fraction, level, previous, cell_frame, evidence_hash = job
    source, output = Path(source_root), Path(output_root)
    raw_summary, summary, compressed = _verified_source(source, relative)
    source_hash = _sha256(compressed)
    summary_hash = _sha256(raw_summary)
    if previous is not None:
        if previous["source_sha256"] != source_hash or previous["source_summary_sha256"] != summary_hash:
            raise ValueError(f"source changed since migration: {relative}")
        dest = output / previous["partition"] / relative
        if _sha256(dest.read_bytes()) != previous["output_summary_sha256"]:
            raise ValueError(f"output summary checksum mismatch: {dest}")
        if _sha256((dest.parent / summary["data_file"]).read_bytes()) != previous["output_sha256"]:
            raise ValueError(f"output shard checksum mismatch: {dest}")
        return previous

    import zstandard
    container = zstandard.ZstdDecompressor().decompress(compressed)
    if _sha256(container) != summary.get("container_sha256"):
        raise ValueError(f"source container checksum mismatch: {relative}")
    if len(container) < _CONTAINER_HEADER.size:
        raise ValueError(f"truncated container: {relative}")
    magic, length = _CONTAINER_HEADER.unpack_from(container)
    end = _CONTAINER_HEADER.size + length
    if magic != _CONTAINER_MAGIC or length <= 0 or end >= len(container):
        raise ValueError(f"invalid container header: {relative}")
    manifest = json.loads(container[_CONTAINER_HEADER.size:end])
    if manifest.get("schema") != EVENT_CACHE_SHARD_SCHEMA:
        raise ValueError(f"unsupported shard schema: {relative}")
    contract = manifest.get("contract")
    if not isinstance(contract, dict) or contract.get("version") != LEGACY_VERSION:
        raise ValueError(f"requires explicit v1 source contract; already-v2/unknown inputs rejected: {relative}")
    allowed_frames = (None, "native", "native.v1") if cell_frame == "native" else (None, "model", "actor_relative.v1")
    if contract.get("action_cell_frame") not in allowed_frames:
        raise ValueError(f"source contract contradicts confirmed cell frame: {relative}")
    records = manifest.get("records")
    if not records or len(records) != manifest.get("frames") or len(records) != summary.get("frames"):
        raise ValueError(f"record/frame count mismatch: {relative}")
    partitions = set()
    perspectives = (PerspectiveTransformV1(0), PerspectiveTransformV1(1))
    for item in records:
        record = EventObservationRecordV1(**item)
        partitions.add(replay_partition(record.replay_tag, fraction))
        if record.target_cell is not None:
            if any(type(v) is not int for v in record.target_cell):
                raise ValueError(f"target_cell must use integer grid coordinates: {relative}")
            if cell_frame == "native":
                item["target_cell"] = perspectives[record.owner]._grid(record.target_cell)
    if len(partitions) != 1:
        raise ValueError(f"mixed train/val replay partitions in {relative}; row splitting is not supported; re-shard input first")
    partition = partitions.pop()
    migrated_contract = dict(contract)
    defaults = default_event_contract()
    for key in ("version", "action_cell_frame", "input_semantics"):
        migrated_contract[key] = defaults[key]
    validate_event_supervised_contract(migrated_contract)
    provenance = {
        "source_summary": relative, "source_sha256": source_hash,
        "source_summary_sha256": summary_hash, "source_contract": contract,
        "transform": TRANSFORM if cell_frame == "native" else "identity; labels already actor-relative",
        "confirmed_cell_frame": cell_frame, "evidence_sha256": evidence_hash,
        "observation_storage": "safetensors payload byte-for-byte unchanged",
    }
    manifest["contract"] = migrated_contract
    manifest["migration"] = provenance
    metadata = _canonical_json(manifest)
    rewritten = _CONTAINER_HEADER.pack(_CONTAINER_MAGIC, len(metadata)) + metadata + container[end:]
    encoded = zstandard.ZstdCompressor(level=level, threads=0).compress(rewritten)
    result = dict(summary)
    result.update(compressed_sha256=_sha256(encoded), container_sha256=_sha256(rewritten),
                  compressed_bytes=len(encoded), container_bytes=len(rewritten),
                  compression_ratio=round(len(rewritten) / max(1, len(encoded)), 6))
    result["migration"] = provenance
    dest = output / partition / relative
    result_bytes = _canonical_json(result)
    _publish(dest.parent / result["data_file"], encoded)
    _publish(dest, result_bytes)
    entry = dict(result, summary_file=relative, data_file=str(Path(relative).parent / result["data_file"]))
    return dict(provenance, partition=partition, output_sha256=result["compressed_sha256"],
                output_summary_sha256=_sha256(result_bytes), contract=migrated_contract, entry=entry)


def migrate(source: str | Path, output: str | Path, *, confirmed_cell_frame: str, evidence_file: str | Path,
            workers: int = 1, val_fraction: float = 0.05, compression_level: int = -5):
    """Migrate a local pool, returning train/val event-cache index dictionaries."""
    if confirmed_cell_frame not in ("native", "model"):
        raise ValueError("--confirmed-cell-frame native|model is mandatory; no convention is inferred")
    evidence = Path(evidence_file).resolve()
    evidence_bytes = evidence.read_bytes()
    if not evidence_bytes.strip():
        raise ValueError("evidence file must be nonempty")
    evidence_hash = _sha256(evidence_bytes)
    if workers < 1 or not math.isfinite(val_fraction) or not 0 < val_fraction < 1:
        raise ValueError("workers must be positive and val_fraction strictly between 0 and 1")
    if not -5 <= compression_level <= 22:
        raise ValueError("compression level must be in -5..22")
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("source and output must be separate, non-nested directories")
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    paths = sorted(str(p.with_name(p.name[:-9] + ".json").relative_to(source))
                   for p in source.rglob("*.crev.zst"))
    if not paths:
        raise ValueError("no source *.crev.zst shards found")
    config = dict(schema="event-cache-native-migration.v1", source=str(source),
                  source_paths_sha256=_sha256(_canonical_json(paths)), shards=len(paths),
                  val_fraction=val_fraction, split="sha256(replay_tag UTF-8)/2**256",
                  compression_level=compression_level,
                  transform=TRANSFORM if confirmed_cell_frame == "native" else "identity; labels already actor-relative",
                  confirmed_cell_frame=confirmed_cell_frame, evidence_sha256=evidence_hash,
                  evidence_file=str(evidence), target_contract=default_event_contract()["version"])
    output.mkdir(parents=True, exist_ok=True)
    # Serialize writers without a stale lock file after interruption.
    import fcntl
    with ExitStack() as stack:
        lock = stack.enter_context((output / ".migration.lock").open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (output / "migration.json").exists() and any(p.name != ".migration.lock" for p in output.iterdir()):
            raise ValueError("output must be empty or an existing matching migration")
        _publish(output / "migration.json", _canonical_json(config))
        _publish(output / "source-evidence", evidence_bytes)
        path_set = set(paths)
        completed = {}
        logs = {}
        for partition in ("train", "val"):
            root = output / partition
            root.mkdir(exist_ok=True)
            log = root / "migration.jsonl"
            if log.exists():
                for line in log.read_bytes().splitlines():
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        raise ValueError(f"incomplete migration journal: {log}; preserve and repair final line before resuming") from exc
                    key = row["source_summary"]
                    if row["partition"] != partition or key in completed or key not in path_set:
                        raise ValueError(f"invalid/duplicate migration journal entry: {key}")
                    completed[key] = row
            logs[partition] = stack.enter_context(log.open("ab"))
        entries = {"train": [], "val": []}
        contract = None
        pool = stack.enter_context(ProcessPoolExecutor(max_workers=workers)) if workers > 1 else None
        # Bound queued work and avoid retaining large source/container buffers.
        for start in range(0, len(paths), workers * 4):
            jobs = [(str(source), str(output), p, val_fraction, compression_level, completed.get(p), confirmed_cell_frame, evidence_hash)
                    for p in paths[start:start + workers * 4]]
            results = pool.map(_migrate_one, jobs) if pool else map(_migrate_one, jobs)
            for row in results:
                if contract is not None and row["contract"] != contract:
                    raise ValueError("source shard contracts differ; migrate homogeneous pools separately")
                contract = row["contract"]
                key, partition = row["source_summary"], row["partition"]
                if key not in completed:
                    stream = logs[partition]
                    stream.write(_canonical_json(row) + b"\n")
                    stream.flush()
                entries[partition].append(row["entry"])
            processed = min(start + workers * 4, len(paths))
            if start == 0 or processed == len(paths) or start // 1000 != processed // 1000:
                for stream in logs.values():
                    os.fsync(stream.fileno())
                print(f"verified {processed}/{len(paths)} shards", file=sys.stderr, flush=True)
        validate_event_supervised_contract(contract)
        indexes = {}
        for partition in ("train", "val"):
            path = output / partition / "index.json"
            # The production writer owns the event schema and self-hash. Write
            # into a temporary directory so existing final indexes never change.
            import tempfile
            with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
                candidate = Path(temporary) / "index.json"
                indexes[partition] = write_event_cache_index(candidate, entries[partition], contract=contract)
                _publish(path, candidate.read_bytes())
        return indexes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirmed-cell-frame", choices=("native", "model"), required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--val-fraction", type=float, default=0.05)
    parser.add_argument("--compression-level", type=int, default=-5)
    args = parser.parse_args(argv)
    try:
        indexes = migrate(args.source, args.output, confirmed_cell_frame=args.confirmed_cell_frame, evidence_file=args.evidence_file,
                          workers=args.workers, val_fraction=args.val_fraction, compression_level=args.compression_level)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"migration failed: {exc}\n")
    print(json.dumps({p: {k: index[k] for k in ("shard_count", "frames", "index_sha256")}
                      for p, index in indexes.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
