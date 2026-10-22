import concurrent.futures
import copy
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from render_web.app import create_app
from render_web.config import Config, write_json
from render_web.store import Store
from render_web import local_render, remote_agent
from render_web.service import Service


def checkpoint(identity="top", **kwargs):
    return dict(
        id=identity,
        run="event-current",
        sha256=identity[0] * 64,
        iteration=171,
        label="RL 171",
        available=True,
        supported=True,
        compatibility="same",
        path="/private/weights/model.pt",
        contract={"internal": True},
        **kwargs,
    )


@pytest.fixture
def api(tmp_path):
    config = Config(data=tmp_path, min_free_bytes=0)
    app = create_app(config, start_worker=False)
    app.state.service.store.replace_checkpoints([checkpoint(), checkpoint("bottom")])
    with TestClient(app) as client:
        yield client, app.state.service.store, config


def test_api_snapshot_persists_across_catalog_refresh_and_restart(api):
    client, store, config = api
    response = client.post("/api/jobs", json=dict(top="top", seed=4294967295))
    assert response.status_code == 202
    job = response.json()
    assert (
        "path" not in job["request"]["top"] and "contract" not in job["request"]["top"]
    )
    assert "worker" not in job
    store.replace_checkpoints([])
    restarted = Store(config.data)
    assert restarted.get(job["id"])["request"]["top"]["sha256"] == "t" * 64
    client.post(f"/api/jobs/{job['id']}/cancel")
    retried = client.post(f"/api/jobs/{job['id']}/retry").json()
    assert retried["request"] == job["request"] and retried["retry_of"] == job["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"top": "missing"},
        {"top": "top", "seed": -1},
        {"top": "top", "seed": 2**32},
        {"top": "top", "seed": "7"},
        {"top": "top", "seed": True},
        {"top": "top", "deck_top": "unknown"},
        {"top": "top", "mode": "versus"},
        {"top": "top", "path": "/etc/passwd"},
    ],
)
def test_invalid_job_rejected_without_enqueue(api, payload):
    client, store, _ = api
    assert client.post("/api/jobs", json=payload).status_code == 422
    assert store.jobs() == []


def test_contract_and_unavailable_archives_rejected(api):
    client, store, _ = api
    a = checkpoint()
    b = checkpoint("bottom")
    b["compatibility"] = "different"
    store.replace_checkpoints([a, b])
    assert (
        client.post(
            "/api/jobs", json=dict(mode="versus", top="top", bottom="bottom")
        ).status_code
        == 422
    )
    a["available"] = False
    store.replace_checkpoints([a])
    assert client.post("/api/jobs", json=dict(top="top")).status_code == 422


def test_disk_guard_and_origin(api):
    client, store, config = api
    config.min_free_bytes = 2**63
    assert client.post("/api/jobs", json=dict(top="top")).status_code == 507
    assert (
        client.post(
            "/api/jobs",
            json=dict(top="top"),
            headers={"Origin": "https://elsewhere.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.get("/api/jobs", headers={"Host": "elsewhere.invalid"}).status_code
        == 403
    )
    assert store.jobs() == []


def test_video_ranges_download_and_file_boundary(api, tmp_path):
    client, store, config = api
    job = client.post("/api/jobs", json=dict(top="top")).json()
    identity = job["id"]
    root = config.data / "jobs" / identity
    root.mkdir(parents=True)
    data = bytes(range(256)) * 16
    (root / "video.mp4").write_bytes(data)
    path = f"/api/jobs/{identity}/files/video.mp4"
    assert client.get(path).status_code == 404
    store.update(identity, state="completed")
    r = client.get(path, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]
    assert r.headers["content-range"] == "bytes 100-199/4096"
    assert client.get(path, headers={"Range": "bytes=-10"}).content == data[-10:]
    assert client.get(path, headers={"Range": "bytes=5000-6000"}).status_code == 416
    assert (
        "attachment"
        in client.get(path + "?download=true").headers["content-disposition"]
    )
    (root / "request.json").write_text("private")
    assert client.get(f"/api/jobs/{identity}/files/request.json").status_code == 404
    assert (
        client.get(f"/api/jobs/{identity}/files/%2e%2e%2frequest.json").status_code
        == 404
    )
    (root / "video.mp4").unlink()
    (root / "video.mp4").symlink_to(root / "request.json")
    assert client.get(path).status_code == 404


def test_fifo_claim_is_atomic_and_queued_cancel_is_terminal(tmp_path):
    store = Store(tmp_path)
    first = store.create({})
    second = store.create({})
    third = store.create({})
    store.cancel(second["id"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(lambda _: Store(tmp_path).claim(), range(8)))
    assert {j["id"] for j in result if j} == {first["id"], third["id"]}
    assert sum(j is not None for j in result) == 2
    assert store.get(second["id"])["state"] == "cancelled"
    assert store.claim() is None


def test_offline_catalog_preserves_last_index(tmp_path, monkeypatch):
    service = Service(Config(data=tmp_path))
    service.store.replace_checkpoints([checkpoint()])

    def offline(*args):
        raise OSError("offline fixture")

    monkeypatch.setattr(service.remote, "call", offline)
    service.refresh()
    assert service.store.checkpoints() == [checkpoint()]
    assert "offline" in service.store.setting("catalog_status")["error"]


def test_scan_excludes_unfinished_and_keeps_migration_and_historical_batches(tmp_path):
    for name, sha in [("event-old", "a" * 64), ("event-residual/nested", "b" * 64)]:
        root = tmp_path / name
        root.mkdir(parents=True)
        write_json(
            root / "fleet.json",
            dict(initial_checkpoint_sha256=sha, start_iteration=171),
        )
        done = root / "iteration-0172"
        done.mkdir()
        write_json(
            done / "success.json",
            dict(checkpoint="/weights/" + "c" * 64 + ".pt", iteration=172),
        )
        (root / "iteration-0173").mkdir()
    raw = subprocess.check_output(
        [sys.executable, "-c", remote_agent.SCAN, str(tmp_path)], text=True
    )
    rows = json.loads(raw)
    assert len(rows) == 4 and {r["iteration"] for r in rows} == {171, 172}
    assert {r["sha256"] for r in rows if r["iteration"] == 171} == {"a" * 64, "b" * 64}


@pytest.fixture
def trace(tmp_path):
    request = dict(
        top={"sha256": "a"},
        bottom={"sha256": "b"},
        seed=123,
        deck_top="golem-beatdown",
        deck_bottom="hog-cycle",
    )
    stats = dict(
        owner_checkpoint_sha256=["a", "b"],
        seed=123,
        decks=["golem-beatdown", "hog-cycle"],
        terminal=dict(
            ended=True, reason="native_final_result", winner=1, terminal_tick=100
        ),
        counters=dict(
            scheduler_version="event-scheduling.v2",
            not_ready=0,
            cancelled_window=0,
            act_violation=0,
            executed=1,
        ),
    )
    actions = [
        dict(
            owner=0,
            world_x=500,
            world_y=1500,
            receipt=dict(ok=True, queued=True, owner=0, cardId=42, executeTick=50),
        )
    ]
    initial = dict(
        players=[
            dict(
                owner=o,
                hand=[{"cardId": n} for n in range(4)],
                cycle=[{"cardId": n} for n in range(4, 8)],
            )
            for o in (0, 1)
        ]
    )
    render = dict(
        schedules=[dict(owner=0, cardId=42, executeTick=50, state="succeeded")],
        per_owner={
            "0": dict(scheduled=1, succeeded=1, failed=0, pending=0),
            "1": dict(scheduled=0, succeeded=0, failed=0, pending=0),
        },
        final_observation=dict(ended=True, winner=1, tick=103),
        final_status=dict(ended=True, tick=103),
    )
    for name, data in [
        ("stats.json", stats),
        ("native-actions.json", actions),
        ("initial.json", initial),
        ("render-initial.json", initial),
        ("render-stats.json", render),
    ]:
        write_json(tmp_path / name, data)
    return tmp_path, request


def test_exact_receipts_and_deals_allow_honest_terminal_tick_difference(trace):
    root, request = trace
    stats, render = local_render.validate_replay(root, request)
    assert render["final_status"]["tick"] - stats["terminal"]["terminal_tick"] == 3


@pytest.mark.parametrize(
    "file,mutate",
    [
        ("stats.json", lambda x: x.update(seed=124)),
        ("stats.json", lambda x: x.update(owner_checkpoint_sha256=["wrong", "b"])),
        ("stats.json", lambda x: x.update(decks=["hog-cycle", "golem-beatdown"])),
        ("stats.json", lambda x: x["terminal"].update(reason="max_ticks")),
        ("native-actions.json", lambda x: x[0]["receipt"].update(ok=False)),
        ("native-actions.json", lambda x: x[0].update(world_x=250)),
        ("render-stats.json", lambda x: x["schedules"][0].update(executeTick=51)),
        ("render-stats.json", lambda x: x["schedules"][0].update(state="pending")),
        ("render-stats.json", lambda x: x["final_observation"].update(winner=0)),
        (
            "render-initial.json",
            lambda x: x["players"][1]["cycle"][0].update(cardId=999),
        ),
    ],
)
def test_mismatched_trace_never_published(trace, file, mutate):
    root, request = trace
    payload = json.loads((root / file).read_text())
    mutate(payload)
    write_json(root / file, payload)
    with pytest.raises(ValueError):
        local_render.validate_replay(root, request)


def test_recovery_does_not_kill_reused_or_unowned_pid(tmp_path, monkeypatch):
    write_json(
        tmp_path / "emulator-owner.json",
        dict(pid=42, birth="old", serial="emulator-5554"),
    )
    monkeypatch.setattr(local_render, "birth", lambda pid: "different-process")

    def forbidden(*a, **kw):
        pytest.fail("sent command to unowned emulator")

    monkeypatch.setattr(local_render.subprocess, "run", forbidden)
    local_render.recover_local(Config(data=tmp_path), tmp_path)


def test_recovery_waits_for_screenrecord_natural_exit(tmp_path, monkeypatch):
    write_json(
        tmp_path / "emulator-owner.json",
        dict(pid=1, birth="owned", serial="emulator-5554"),
    )
    write_json(
        tmp_path / "recording.json",
        dict(pid=2, birth="recording", start_time=0, limit=5),
    )
    ticks = [0]
    commands = []
    cleanup_times = []

    def alive(owner):
        return owner["pid"] == 1 and not commands or owner["pid"] == 2 and ticks[0] < 5

    monkeypatch.setattr(local_render, "live", alive)
    monkeypatch.setattr(local_render.time, "time", lambda: ticks[0])
    monkeypatch.setattr(
        local_render.time, "sleep", lambda n: ticks.__setitem__(0, ticks[0] + n)
    )
    monkeypatch.setattr(
        local_render.subprocess, "run", lambda args, **kw: commands.append(args)
    )
    monkeypatch.setattr(
        local_render.Renderer, "cleanup_guest_storage",
        lambda self, phase: cleanup_times.append((phase, ticks[0])),
    )
    local_render.recover_local(Config(data=tmp_path), tmp_path)
    assert ticks[0] >= 5 and len(commands) == 1 and commands[0][-2:] == ["emu", "kill"]
    assert cleanup_times == [("recovery", 5)]


def test_cancel_terminates_only_owned_export_child(tmp_path):
    with pytest.raises(local_render.Cancelled):
        local_render.run_export_process(
            tmp_path,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            lambda: True,
            "export.log",
            40,
        )
    owned = json.loads((tmp_path / "export-owner.json").read_text())
    assert not local_render.live(owned)


def test_export_process_reports_failed_decode(tmp_path):
    with pytest.raises(RuntimeError, match="decode.log"):
        local_render.run_export_process(
            tmp_path,
            [sys.executable, "-c", "raise SystemExit(1)"],
            lambda: False,
            "decode.log",
            5,
        )
