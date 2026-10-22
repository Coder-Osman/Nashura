"""Local checkpoint inventory with cached contract inspection."""

import hashlib
import json
from pathlib import Path
import threading


class Checkpoints:
    def __init__(self, root: Path, data: Path, *, remote=None):
        self.root, self.data = root, data
        self.lock = threading.RLock()
        self.cache = {}
        self.extra_file = data / "checkpoint-paths.json"
        self.extra = json.loads(self.extra_file.read_text()) if self.extra_file.exists() else []
        from .remote_weights import RemoteWeights
        self.remote = remote or RemoteWeights(data)

    def start(self):
        self.remote.start()

    def close(self):
        self.remote.close()

    def status(self):
        return self.remote.snapshot()

    def refresh_async(self):
        self.remote.refresh_async()

    def inspect(self, path: Path) -> dict:
        import torch
        from native_runner.training.event_checkpoint import _resolve, _contract, _contract_hash

        path = path.resolve()
        stat = path.stat()
        signature = (str(path), stat.st_mtime_ns, stat.st_size)
        if signature in self.cache:
            return dict(self.cache[signature])
        row = dict(id=hashlib.sha256(str(path).encode()).hexdigest()[:24],
                   path=str(path), name=path.name, label=path.stem,
                   bytes=stat.st_size, supported=False, source="local", cached=True, available=True)
        try:
            payload = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
            backbone, model, event = _resolve(payload, None, None)
            if not backbone.snapshot_only:
                raise ValueError("The live runner requires snapshot_only")
            if payload.get("event_initialization", {}).get("needs_recalibration_or_finetune"):
                raise ValueError("Checkpoint still requires finetuning")
            contract_hash = _contract_hash(_contract(backbone, model, event))
            if payload.get("contract_sha256", contract_hash) != contract_hash:
                raise ValueError("Checkpoint contract hash mismatch")
            if not isinstance(payload.get("model"), dict):
                raise ValueError("Checkpoint is missing model weights")
            iteration = payload.get("event_ppo_training", {}).get("iterations")
            row.update(supported=True, iteration=iteration, residual_blocks=model.residual_blocks,
                       version=payload["event_checkpoint_contract"]["version"],
                       label=f"RL {iteration}" if iteration is not None else path.stem)
        except Exception as exc:
            row["reason"] = str(exc)[:350]
        self.cache[signature] = row
        return dict(row)

    def items(self) -> list[dict]:
        with self.lock:
            paths = set((self.root / "checkpoints").rglob("*.pt"))
            paths.update(Path(p) for p in self.extra)
            rows = []
            for path in sorted(paths):
                if path.is_file():
                    rows.append(self.inspect(path))
            rows.extend(self.remote.items())
            return sorted(rows, key=lambda r: (r["supported"], r.get("iteration") or 0, r["name"]), reverse=True)

    def get(self, identity: str) -> dict:
        row = next((r for r in self.items() if r["id"] == identity), None)
        if row is None:
            raise ValueError("Checkpoint is no longer available")
        if not row["supported"]:
            raise ValueError(row.get("reason", "Unsupported checkpoint"))
        if not row.get("available", True):
            raise ValueError("权重归档暂不可用，请刷新目录")
        return row

    def prepare(self, row, *, stop, progress):
        if row.get("source") != "remote":
            return row
        path = self.remote.fetch(row, stop=stop, progress=progress)
        if stop.is_set():
            raise InterruptedError("已停止权重加载")
        with self.lock:
            inspected = self.inspect(path)
        if not inspected["supported"]:
            raise ValueError("所选权重未通过本机契约校验：" + inspected.get("reason", ""))
        return {**row, "path": str(path), "cached": True, "bytes": inspected["bytes"],
                "version": inspected["version"], "residual_blocks": inspected["residual_blocks"]}

    def add(self, value: str) -> dict:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".pt", ".pth"}:
            raise ValueError("Select an existing .pt or .pth file")
        with self.lock:
            row = self.inspect(path)
            if not row["supported"]:
                raise ValueError(row["reason"])
            if str(path) not in self.extra:
                self.extra.append(str(path))
                tmp = self.extra_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.extra))
                tmp.replace(self.extra_file)
            return row
