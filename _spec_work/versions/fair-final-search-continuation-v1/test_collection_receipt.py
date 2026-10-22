from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect_continuation import ARCHIVE_LOGS, publish_download_receipt


def test_recollection_preserves_pinned_receipt_bytes_and_timestamp(tmp_path, monkeypatch):
    files = {"continue.log": dict(bytes=12, sha256="a" * 64)}
    original = publish_download_receipt(tmp_path, remote="/attempt", files=files)
    path = tmp_path / "download-receipt.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr("collect_continuation.time.time", lambda: original["timestamp"] + 100)
    repeated = publish_download_receipt(tmp_path, remote="/attempt", files=deepcopy(files))
    assert repeated == original
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("damage", ["remote", "sha", "size", "new_log", "admissible"])
def test_changed_inventory_never_rewrites_pinned_receipt(tmp_path, damage):
    files = {"worker.log": dict(bytes=12, sha256="a" * 64)}
    publish_download_receipt(tmp_path, remote="/attempt", files=files)
    path = tmp_path / "download-receipt.json"
    remote = "/attempt"
    if damage == "remote":
        remote = "/other-attempt"
    elif damage == "sha":
        files["worker.log"]["sha256"] = "b" * 64
    elif damage == "size":
        files["worker.log"]["bytes"] += 1
    elif damage == "new_log":
        files["continue.log"] = dict(bytes=1, sha256="c" * 64)
    else:
        previous = json.loads(path.read_text())
        previous["training_admissible"] = True
        path.write_text(json.dumps(previous))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="pinned download receipt differs"):
        publish_download_receipt(tmp_path, remote=remote, files=files)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_archive_includes_continuation_log():
    assert "continue.log" in ARCHIVE_LOGS
