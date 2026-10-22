"""Reclaim guest recording copies only after a verified host copy exists."""

import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import time

from .config import write_json


RECORDING_NAME = re.compile(r"render-web-([0-9a-f]{32})\.mp4")
GUEST_MEDIA = "/data/media/0"


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cleanup_guest_recordings(config, root, adb, *, phase):
    """Caller must own an idle guest. Leave originals intact on any copy error.

    Delete the exact verified file through the root-only backing directory,
    and sync the filesystem before the caller shuts down the emulator.
    Never erase application data, resources, or unrelated shared storage.
    """
    report = dict(phase=phase, time=time.time(), files=[], errors=[])

    def checked(*args, timeout=30):
        result = adb(*args, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "ADB command failed").strip())
        return result.stdout.strip()

    def free_bytes():
        raw = checked("shell", "stat", "-f", "-c", "%a:%S", "/data", timeout=5)
        available, size = map(int, raw.split(":"))
        if available < 0 or size <= 0:
            raise ValueError("invalid guest filesystem statistics")
        return available * size

    try:
        recorder = adb("shell", "pidof", "screenrecord", timeout=5)
        if recorder.returncode not in (0, 1) or recorder.stderr.strip():
            raise RuntimeError("guest recorder state unavailable")
        if recorder.stdout.strip():
            report["deferred"] = "screenrecord is still running"
            return report
        report["free_before"] = free_bytes()
        paths = checked(
            "shell",
            "su 0 find /data/media/0 -maxdepth 1 -type f -name 'render-web-*.mp4'",
        ).splitlines()
        for remote_path in sorted(paths):
            name = remote_path.removeprefix(GUEST_MEDIA + "/")
            match = RECORDING_NAME.fullmatch(name)
            if not match or remote_path != GUEST_MEDIA + "/" + name:
                continue
            item = dict(guest=remote_path, removed=False)
            report["files"].append(item)
            try:
                digest = checked("shell", "su", "0", "sha256sum", remote_path, timeout=120).split()[0]
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid guest recording checksum")
                item["sha256"] = digest
                destination = config.data / "jobs" / match[1] / "native-full.mp4"
                verified = destination.is_file() and file_sha256(destination) == digest
                if not verified:
                    # Interrupted or deleted jobs still get an archival copy. A
                    # different existing host recording must never be overwritten.
                    destination = config.data / "guest-recording-archive" / f"{name[:-4]}-{digest}.mp4"
                    verified = destination.is_file() and file_sha256(destination) == digest
                if not verified:
                    size = int(checked("shell", "su", "0", "stat", "-c", "%s", remote_path))
                    if shutil.disk_usage(config.data).free < size + config.min_free_bytes:
                        raise RuntimeError("本机空间不足以保存模拟器遗留录像")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    partial = destination.with_suffix(".mp4.partial")
                    checked("pull", "/sdcard/" + name, str(partial), timeout=120)
                    if file_sha256(partial) != digest:
                        raise ValueError("录像副本 SHA-256 核验失败，保留模拟器原件")
                    partial.replace(destination)
                item["host"] = str(destination)
                item["bytes"] = destination.stat().st_size
                checked("shell", "su", "0", "rm", "-f", remote_path)
                checked("shell", "su", "0", "test", "!", "-e", remote_path)
                item["removed"] = True
            except (OSError, ValueError, IndexError, RuntimeError, subprocess.SubprocessError) as exc:
                item["error"] = str(exc)
                report["errors"].append(dict(guest=remote_path, error=str(exc)))
        if any(item["removed"] for item in report["files"]):
            checked("shell", "su", "0", "sync", timeout=30)
            report["synced"] = True
        report["free_after"] = free_bytes()
        report["reclaimed_bytes"] = report["free_after"] - report["free_before"]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["errors"].append(dict(error=str(exc)))
    finally:
        write_json(Path(root) / f"guest-cleanup-{phase}.json", report)
    return report
