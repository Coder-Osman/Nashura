"""Remote catalog and verified local weights using the video dashboard's SSH configuration."""

from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time

from render_web.config import Config
from render_web.checkpoint_sources import checkpoint_location
from render_web.remote import Remote
from render_web.store import Store


class RemoteWeights:
    def __init__(self, data, *, config=None, remote=None, store=None, refresh=None):
        self.config = config or Config.load()
        self.remote = remote or Remote(self.config)
        self.store = store or Store(self.config.data)
        self.refresh_callback = refresh
        self.directory = self.config.data / "weights"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.download_lock = threading.Lock()

    @contextmanager
    def weight_lock(self, sha, stop):
        with (self.directory / (sha + ".lock")).open("a+") as lock:
            while True:
                if stop.is_set():
                    raise InterruptedError("已停止权重下载")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    stop.wait(.1)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def normalize(self, entries):
        result = []
        for entry in entries:
            sha = entry.get("sha256", "")
            try:
                expected, _ = checkpoint_location(self.config.archive_root, entry)
            except (ValueError, TypeError):
                continue
            path = entry.get("remote_path", entry.get("path"))
            if path != str(expected):
                continue
            contract = entry.get("contract", {})
            supported = bool(entry.get("supported") and
                contract.get("version") in {"event-checkpoint.v2", "event-checkpoint.v3"} and
                contract.get("backbone_config", {}).get("snapshot_only") is True)
            result.append({**entry, "id": entry["id"],
                           "remote_path": str(expected), "path": None, "source": "remote", "supported": supported,
                           "version": contract.get("version"),
                           "residual_blocks": contract.get("model_config", {}).get("residual_blocks", 0),
                           "name": f"rl-{entry.get('iteration', 0)}-{sha[:8]}.pt"})
        return result

    def items(self):
        entries = self.normalize(self.store.checkpoints())
        rows = []
        for entry in entries:
            path = self.directory / (entry["sha256"] + ".pt")
            cached = path.is_file()
            rows.append({**entry, "cached": cached, "path": str(path) if cached else None,
                         "available": cached or bool(entry.get("available")),
                         "bytes": path.stat().st_size if cached else None})
        return rows

    def snapshot(self):
        return {**self.store.setting("catalog_status", {}), "host": self.config.ssh_host}

    def refresh_async(self):
        if self.refresh_callback:
            self.refresh_callback()
        else:
            raise RuntimeError("请从合并后的 CR 工作台刷新权重")

    def start(self):
        pass

    def close(self):
        pass

    def fetch(self, row, *, stop, progress):
        validated = self.normalize([row])
        if not validated or not validated[0]["supported"]:
            raise ValueError("远端权重目录条目无效")
        row = validated[0]
        path = self.directory / (row["sha256"] + ".pt")

        def check():
            if stop.is_set():
                raise InterruptedError("已停止权重下载")

        with self.download_lock, self.weight_lock(row["sha256"], stop):
            check()
            if path.exists():
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        check()
                        digest.update(chunk)
                if digest.hexdigest() == row["sha256"]:
                    return path
            with self.remote.connect() as client, client.open_sftp() as sftp:
                sftp.get_channel().settimeout(10)
                size = sftp.stat(row["remote_path"]).st_size
                if shutil.disk_usage(self.directory).free < size + self.config.min_free_bytes:
                    raise ValueError("本机剩余空间不足以缓存所选权重")
                progress(0, size)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".download", delete=False) as output:
                        temporary = Path(output.name)
                        digest, received, reported = hashlib.sha256(), 0, 0.0
                        with sftp.open(row["remote_path"], "rb") as source:
                            source.prefetch(file_size=size, max_concurrent_requests=8)
                            while True:
                                check()
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                output.write(chunk)
                                digest.update(chunk)
                                received += len(chunk)
                                now = time.monotonic()
                                if now - reported >= .25:
                                    progress(received, size)
                                    reported = now
                    check()
                    if received != size or digest.hexdigest() != row["sha256"]:
                        raise ValueError("远端权重 SHA256 校验失败，未用于对局")
                    temporary.replace(path)
                    progress(size, size)
                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()
        return path
