"""Bounded, round-local reuse of verified decoded shards; content is rehashed on every access."""
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading


class VerifiedShardCache:
    def __init__(self, max_bytes=2 * 1024**3):
        if max_bytes < 0:
            raise ValueError("negative shard cache budget")
        self.max_bytes = max_bytes
        self.bytes = 0
        self.items = OrderedDict()
        self.pending = {}
        self.lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    def load(self, summary_path, *, verify=True):
        from .event_cache import load_event_rl_shard, EVENT_RL_SUMMARY_SCHEMA
        if not verify:
            raise ValueError("decoded reuse requires SHA verification")
        path = Path(summary_path)
        raw = path.read_bytes()
        summary = json.loads(raw)
        name = summary.get("data_file")
        if (summary.get("schema") != EVENT_RL_SUMMARY_SCHEMA or not isinstance(name, str)
                or Path(name).name != name):
            raise ValueError("invalid event cache summary")
        data_path = path.parent / name
        compressed = data_path.read_bytes()
        digest = hashlib.sha256(compressed).hexdigest()
        if digest != summary.get("compressed_sha256"):
            raise ValueError("event cache shard checksum mismatch")
        key = (hashlib.sha256(raw).hexdigest(), digest)
        with self.lock:
            if key in self.items:
                shard, _ = self.items[key]
                self.items.move_to_end(key)
                self.hits += 1
                return replace(shard, path=data_path)
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
                self.misses += 1
            else:
                self.hits += 1
        if not owner:
            return replace(future.result(), path=data_path)
        try:
            shard = load_event_rl_shard(path, verify=True, _source=(summary, compressed))
            # Bound decompressed tensors plus a conservative Python record allowance.
            size = max(int(summary.get("container_bytes", 0)), len(compressed)) + len(shard.records)*4096
            with self.lock:
                if size <= self.max_bytes:
                    while self.bytes + size > self.max_bytes and self.items:
                        _, (_, evicted_size) = self.items.popitem(last=False)
                        self.bytes -= evicted_size
                        self.evictions += 1
                    self.items[key] = (shard, size)
                    self.bytes += size
                future.set_result(shard)
            return shard
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def clear(self):
        with self.lock:
            if self.pending:
                raise RuntimeError("shard cache still has active loads")
            self.items.clear()
            self.bytes = 0

    def as_dict(self):
        with self.lock:
            return dict(hits=self.hits, misses=self.misses, evictions=self.evictions,
                        bytes=self.bytes, max_bytes=self.max_bytes)
