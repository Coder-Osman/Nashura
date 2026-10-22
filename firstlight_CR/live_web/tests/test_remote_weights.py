from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import threading

from fastapi.testclient import TestClient
import pytest

from live_web.remote_weights import RemoteWeights
from live_web.service import Service as LiveService
from render_web.app import create_app
from render_web.config import Config
from render_web import checkpoint_sources as sources
from render_web.store import Store


def entry(config, payload=b"checkpoint"):
    sha = hashlib.sha256(payload).hexdigest()
    return dict(id="shared-id", sha256=sha, run="event-test", supported=True,
                available=True, iteration=123, label="RL 123",
                path=f"{config.archive_root}/event-test/weights/{sha}.pt",
                contract=dict(version="event-checkpoint.v3",
                              backbone_config=dict(snapshot_only=True),
                              model_config=dict(residual_blocks=2)))


class FakeRemote:
    def __init__(self, data=b"checkpoint", on_read=None):
        self.data = data
        self.connections = 0
        self.on_read = on_read

    @contextmanager
    def connect(self):
        self.connections += 1
        yield self

    @contextmanager
    def open_sftp(self):
        yield self

    def get_channel(self):
        return SimpleNamespace(settimeout=lambda timeout: None)

    def stat(self, path):
        return SimpleNamespace(st_size=len(self.data))

    @contextmanager
    def open(self, path, mode):
        source = io.BytesIO(self.data)
        source.prefetch = lambda **kwargs: None
        original_read = source.read

        def read(size):
            data = original_read(size)
            if self.on_read:
                self.on_read()
            return data

        source.read = read
        yield source


@pytest.fixture
def catalog(tmp_path):
    config = Config(data=tmp_path / "shared", min_free_bytes=0)
    store = Store(config.data)
    store.replace_checkpoints([entry(config)])
    remote = FakeRemote()
    return RemoteWeights(tmp_path / "live", config=config, store=store, remote=remote)


def test_same_catalog_identity_and_refresh_owner(catalog):
    calls = []
    catalog.refresh_callback = lambda: calls.append(True)
    catalog.refresh_async()
    assert calls == [True]
    assert catalog.items()[0]["id"] == catalog.store.checkpoints()[0]["id"]
    catalog.store.set_setting("catalog_status", dict(updated=123, refreshing=True))
    assert catalog.snapshot()["updated"] == 123
    assert catalog.snapshot()["refreshing"]
    catalog.store.replace_checkpoints([])
    assert catalog.items() == []


def test_download_once_and_share_cache_across_instances(catalog, tmp_path):
    progress = []
    row = catalog.items()[0]
    args = dict(stop=threading.Event(), progress=lambda *values: progress.append(values))
    path = catalog.fetch(row, **args)
    assert path.parent == catalog.config.data / "weights"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    assert progress[-1] == (10, 10)
    second = RemoteWeights(tmp_path / "different-page", config=catalog.config,
                           store=catalog.store, remote=catalog.remote)
    assert second.fetch(second.items()[0], **args) == path
    assert catalog.remote.connections == 1
    assert second.items()[0]["cached"]


def test_bad_digest_does_not_publish_file(catalog):
    catalog.remote.data = b"wrong-data"
    with pytest.raises(ValueError, match="SHA256"):
        catalog.fetch(catalog.items()[0], stop=threading.Event(), progress=lambda *args: None)
    assert not list(catalog.directory.glob("*.pt"))
    assert not list(catalog.directory.glob("*.download"))


def test_cancel_during_download_removes_partial(catalog):
    stop = threading.Event()
    catalog.remote.on_read = stop.set
    with pytest.raises(InterruptedError):
        catalog.fetch(catalog.items()[0], stop=stop, progress=lambda *args: None)
    assert not list(catalog.directory.glob("*.pt"))
    assert not list(catalog.directory.glob("*.download"))


def test_simultaneous_requests_download_only_once(catalog, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time

    catalog.remote.on_read = lambda: time.sleep(.1)
    other = RemoteWeights(tmp_path / "other", config=catalog.config,
                          store=catalog.store, remote=catalog.remote)
    row = catalog.items()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(c.fetch, row, stop=threading.Event(), progress=lambda *args: None)
                   for c in (catalog, other)]
        results = [f.result(timeout=3) for f in futures]
    assert results[0] == results[1]
    assert catalog.remote.connections == 1


def test_corrupted_cache_is_repaired(catalog):
    row = catalog.items()[0]
    path = catalog.directory / (row["sha256"] + ".pt")
    path.write_bytes(b"corrupted")
    assert catalog.fetch(row, stop=threading.Event(), progress=lambda *args: None) == path
    assert path.read_bytes() == b"checkpoint"


def test_remote_path_is_confined_to_archive(catalog):
    row = catalog.items()[0]
    for patch in [{"remote_path": "/etc/passwd"}, {"run": "../outside"}, {"sha256": "../bad"}]:
        with pytest.raises(ValueError, match="条目"):
            catalog.fetch({**row, **patch}, stop=threading.Event(), progress=lambda *args: None)
    assert catalog.remote.connections == 0


def test_settlement_live_weight_uses_same_identity_and_verified_download(catalog):
    row = entry(catalog.config)
    row.update(run=sources.SETTLEMENT_RUN, category=sources.SETTLEMENT_CATEGORY,
               category_label=sources.SETTLEMENT_LABEL)
    row["path"] = str(sources.checkpoint_location(catalog.config.archive_root, row)[0])
    catalog.store.replace_checkpoints([row])
    visible = catalog.items()[0]
    assert visible["id"] == row["id"] and visible["remote_path"] == row["path"]
    path = catalog.fetch(visible, stop=threading.Event(), progress=lambda *args: None)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    for patch in ({"run":"another-run"}, {"category":"formal"},
                  {"remote_path":row["path"].replace("/training/", "/benchmark-216/")}):
        assert catalog.normalize([{**visible, **patch}]) == []


def test_download_failure_prevents_game_navigation(tmp_path, monkeypatch):
    class FailedCatalog:
        def get(self, identity):
            return dict(id=identity, source="remote", label="RL 123")

        def prepare(self, *args, **kwargs):
            raise ValueError("fixture checksum failure")

    service = LiveService(data=tmp_path, catalog=FailedCatalog())
    service.device.serial = "test-remote-download"
    monkeypatch.setattr(service.device, "home", lambda stop: pytest.fail("Unexpected game control"))
    service.start("chosen")
    service.worker.join(2)
    assert service.current["state"] == "failed"
    assert "checksum" in service.current["message"]


def test_merged_routes_use_one_catalog_and_one_refresher(tmp_path, monkeypatch):
    config = Config(data=tmp_path, min_free_bytes=0)
    app = create_app(config, start_worker=False)
    store = app.state.service.store
    store.replace_checkpoints([entry(config)])
    remote = app.state.live_service.catalog.remote
    assert remote.store is store
    assert remote.remote is app.state.service.remote
    assert remote.refresh_callback.__self__ is app.state.service
    calls = []
    monkeypatch.setattr(app.state.service, "refresh", lambda: calls.append(True))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/live/").status_code == 200
        assert client.get("/live/static/app.js").status_code == 200
        shared = client.get("/api/checkpoints").json()["items"]
        live = client.get("/live/api/checkpoints").json()["items"]
        assert shared[0]["id"] in {r["id"] for r in live}
        assert client.get("/api/health").json()["features"] == ["video", "live", "shared-checkpoints"]
        response = client.post("/live/api/checkpoints/refresh", json={}, headers={"X-Live-Control": "1"})
        assert response.status_code == 202
        assert calls == [True]
        assert not client.get("/live/api/status").json()["busy"]
