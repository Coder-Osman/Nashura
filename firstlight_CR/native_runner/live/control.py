"""Exclusive policy control of a single live device."""

from contextlib import contextmanager
import fcntl
from pathlib import Path
import re


@contextmanager
def device_lease(serial: str):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
        raise ValueError("invalid device serial")
    directory = Path(__file__).resolve().parents[2] / "artifacts/live-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (serial + ".lock")).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another policy runner already controls this device") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
