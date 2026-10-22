import json
from pathlib import Path
import sys
import types
import socket

import pytest

from render_web import catalog_metadata, remote_agent, runner, service
from render_web import checkpoint_sources as sources
from render_web.config import Config, write_json
from render_web.store import Store
from render_web.generate_match import validate_rollout


def test_catalog_archives_duplicate_sha_and_migrated_identity(
    tmp_path, monkeypatch, capsys
):
    archive = tmp_path / "archive"
    source = tmp_path / "entries.json"
    cache = tmp_path / "cache.json"
    settlement_root = tmp_path / "data/training"
    monkeypatch.setattr(sources, "SETTLEMENT_ROOT", str(settlement_root))
    rows = [
        dict(run="event-old", sha256="a" * 64, iteration=171, initial=False),
        dict(run="event-old", sha256="a" * 64, iteration=171, initial=True),
        dict(run="event-residual-new", sha256="b" * 64, iteration=171, initial=True),
        dict(run="event-offline", sha256="c" * 64, iteration=40, initial=False),
        dict(run="event-legacy", sha256="d" * 64, iteration=10, initial=False),
        dict(run="event-old/tower-trial", sha256="a" * 64, iteration=171, initial=False,
             category="tower-beta1-formal1553", category_label="塔血实验 β=1 · 1553 起点",
             trial_round=1, trial_updates=200),
        dict(run=sources.SETTLEMENT_RUN, sha256="e" * 64, iteration=171, initial=False,
             category=sources.SETTLEMENT_CATEGORY, category_label=sources.SETTLEMENT_LABEL,
             label_prefix="近静场结算 β=1/3", trial_round=8, trial_updates=200),
    ]
    for row in rows:
        if row["run"] == "event-offline":
            continue
        path = Path(sources.checkpoint_location(archive, row)[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    write_json(source, rows)
    loads = []

    def load(path, **kwargs):
        loads.append(path)
        return dict(
            event_checkpoint_contract=dict(
                version=(
                    "event-checkpoint.v3"
                    if path.stem[0] == "b"
                    else "event-checkpoint.v2"
                ),
                input_semantics="same",
                action_semantics="same",
                backbone_config={"snapshot_only": True},
                event_config={},
            ),
            event_ppo_training={"iterations": 171},
            legacy=path.stem[0] == "d",
        )

    torch = types.ModuleType("torch")
    torch.load = load
    torch.set_num_threads = lambda _: None
    checkpoint = types.ModuleType("native_runner.training.event_checkpoint")

    def resolve(payload, *args):
        if payload["legacy"]:
            raise ValueError("unsupported legacy contract")
        return types.SimpleNamespace(snapshot_only=True), None, None

    checkpoint._resolve = resolve
    checkpoint._contract_hash = lambda _: "contract-hash"
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(
        sys.modules, "native_runner.training.event_checkpoint", checkpoint
    )
    monkeypatch.setattr(
        sys, "argv", ["catalog", str(tmp_path), str(source), str(archive), str(cache)]
    )
    catalog_metadata.main()
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 6 and len(loads) == 4
    settlement = next(x for x in result if x["category"] == sources.SETTLEMENT_CATEGORY)
    assert settlement["label"] == "近静场结算 β=1/3 · RL 171（8/200）"
    assert settlement["path"] == str(settlement_root / "weights" / ("e" * 64 + ".pt"))
    trial = next(x for x in result if x.get("trial_round") == 1)
    assert trial["category"] == "tower-beta1-formal1553"
    assert "塔血 β=1" in trial["label"] and "1/200" in trial["label"]
    assert trial["path"] == str(archive / "event-old/tower-trial/weights" / ("a" * 64 + ".pt"))
    old = next(x for x in result if x["run"] == "event-old")
    migrated = next(x for x in result if x["run"] == "event-residual-new")
    assert (
        old["id"] != migrated["id"]
        and old["compatibility"] == migrated["compatibility"]
    )
    assert "残差迁移" in migrated["label"]
    assert not next(x for x in result if x["run"] == "event-offline")["available"]
    assert not next(x for x in result if x["run"] == "event-legacy")["supported"]
    catalog_metadata.main()
    capsys.readouterr()
    assert (
        len(loads) == 4
    )  # immutable metadata cache is reused, including rejection reasons


def test_remote_start_is_idempotent_and_never_relaunches_interrupted_job(
    tmp_path, monkeypatch
):
    cfg = {"remote_root": str(tmp_path)}
    identity = "a" * 32
    root = tmp_path / "jobs" / identity
    root.mkdir(parents=True)
    monkeypatch.setattr(
        remote_agent, "live", lambda owner: bool(owner and owner.get("alive"))
    )

    def forbidden(*a, **kw):
        pytest.fail("duplicate generator launched")

    monkeypatch.setattr(remote_agent.subprocess, "Popen", forbidden)
    for state in ("starting", "generating", "completed", "failed", "cancelled"):
        write_json(root / "status.json", dict(state=state, owner={"alive": True}))
        assert remote_agent.start(cfg, identity)["state"] == state
    write_json(root / "status.json", dict(state="generating", owner={"alive": False}))
    assert remote_agent.start(cfg, identity)["state"] == "interrupted"


def test_service_reclaims_only_dead_worker_and_preserves_live_worker(
    tmp_path, monkeypatch
):
    svc = service.Service(Config(data=tmp_path))
    first = svc.store.create({})
    svc.store.claim()
    svc.store.update(first["id"], worker={"pid": 1, "birth": "owned"})
    queued = svc.store.create({})
    calls = []
    monkeypatch.setattr(service, "live", lambda owner: bool(owner))
    svc.stop.wait = lambda seconds: svc.stop.set()
    svc.loop()
    assert svc.store.get(queued["id"])["state"] == "queued"
    svc.stop.clear()
    monkeypatch.setattr(service, "live", lambda owner: False)
    monkeypatch.setattr(
        svc.remote,
        "call",
        lambda command, identity: calls.append((command, identity)) or {},
    )
    monkeypatch.setattr(
        service,
        "recover_local",
        lambda config, root: calls.append(("local", root.name)),
    )
    svc.loop()
    assert svc.store.get(first["id"])["state"] == "interrupted"
    assert calls == [("recover", first["id"]), ("local", first["id"])]
    assert svc.store.get(queued["id"])["state"] == "queued"


def test_disconnect_after_submit_queries_same_remote_id(tmp_path, monkeypatch):
    config = Config(data=tmp_path, min_free_bytes=0)
    store = Store(tmp_path)
    job = store.create(dict(top={}, bottom={}, seed=123))
    store.claim()
    calls = []

    class Remote:
        def __init__(self, config):
            pass

        def submit(self, j):
            calls.append(("submit", j["id"]))
            if len(calls) == 1:
                raise OSError("response lost after start")
            return {"state": "generating"}

        def call(self, command, identity):
            calls.append((command, identity))
            return {"state": "completed", "pending": False}

        def fetch(self, identity, root):
            calls.append(("fetch", identity))

    class Renderer:
        def __init__(self, *args):
            pass

        def render(self):
            pass

    monkeypatch.setattr(runner, "Remote", Remote)
    monkeypatch.setattr(runner, "Renderer", Renderer)
    monkeypatch.setattr(runner, "validate_trace", lambda *a: None)
    monkeypatch.setattr(runner, "recover_local", lambda *a: None)
    monkeypatch.setattr(runner, "export", lambda *a: {"verified": True})
    monkeypatch.setattr(runner.time, "sleep", lambda *a: None)
    runner.run(config, job["id"])
    assert store.get(job["id"])["state"] == "completed"
    assert {identity for _, identity in calls} == {job["id"]}
    assert [command for command, _ in calls] == ["submit", "submit", "status", "fetch"]


def test_cancel_before_start_never_contacts_remote(tmp_path, monkeypatch):
    config = Config(data=tmp_path, min_free_bytes=0)
    store = Store(tmp_path)
    job = store.create({})
    store.claim()
    store.cancel(job["id"])
    monkeypatch.setattr(runner, "recover_local", lambda *a: None)

    class Remote:
        def __init__(self, *args):
            pass

        def submit(self, *args):
            pytest.fail("cancelled job started")

    monkeypatch.setattr(runner, "Remote", Remote)
    runner.run(config, job["id"])
    assert store.get(job["id"])["state"] == "cancelled"


def test_failed_cleanup_stays_pending_across_offline_recovery(tmp_path, monkeypatch):
    svc = service.Service(Config(data=tmp_path))
    job = svc.store.create({})
    svc.store.update(job["id"], state="failed", progress={"cleanup_pending": True})
    monkeypatch.setattr(service, "live", lambda _: False)

    def offline(*a):
        raise OSError("remote unavailable")

    monkeypatch.setattr(svc.remote, "call", offline)
    svc.stop.wait = lambda _: svc.stop.set()
    svc.loop()
    assert svc.store.get(job["id"])["progress"]["cleanup_pending"] is True


def test_slow_attestation_keeps_single_request_open(monkeypatch):
    sent = []
    reads = [socket.timeout(), socket.timeout(), b'{"ok":true}\n']

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def sendall(self, data):
            sent.append(data)

        def recv(self, size):
            value = reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(
        remote_agent.socket, "create_connection", lambda *a, **kw: Connection()
    )
    assert remote_agent.control(1234, "attest", timeout=90) == {"ok": True}
    assert sent == [b"attest\n"] and not reads


def test_waiting_attestation_observes_cancellation(monkeypatch):
    closed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def settimeout(self, value):
            pass

        def sendall(self, data):
            pass

        def recv(self, size):
            pytest.fail("received after cancellation")

    def cancel():
        raise InterruptedError("cancelled")

    monkeypatch.setattr(
        remote_agent.socket, "create_connection", lambda *a, **kw: Connection()
    )
    with pytest.raises(InterruptedError):
        remote_agent.control(1234, "attest", cancelled=cancel)
    assert closed == [True]


def test_cross_checkpoint_game_keeps_both_actions_but_only_learner_rows():
    counters = dict(
        terminal=True,
        scheduler_version="event-scheduling.v2",
        not_ready=0,
        cancelled_window=0,
        act_violation=0,
        executed=105,
    )
    terminal = dict(reason="native_final_result")
    rows = {0: [types.SimpleNamespace(done=True, truncated=False)], 1: []}
    validate_rollout(counters, terminal, rows, 105, same_model=False)
    with pytest.raises(ValueError, match="计数"):
        validate_rollout(counters, terminal, rows, 53, same_model=False)
    with pytest.raises(ValueError, match="终局"):
        validate_rollout(counters, terminal, rows, 105, same_model=True)
    rows[0][0].truncated = True
    with pytest.raises(ValueError, match="截断"):
        validate_rollout(counters, terminal, rows, 105, same_model=False)


def test_other_session_using_same_avd_on_different_port_is_busy():
    lines = (
        "/sdk/qemu/qemu-system-x86_64-headless -avd firstlight-video251 -port 5582\n"
    )
    assert remote_agent.avd_process_running(lines, "firstlight-video251")
    assert not remote_agent.avd_process_running(lines, "different-video-avd")
    assert not remote_agent.avd_process_running(
        'python3 -c "qemu -avd firstlight-video251"', "firstlight-video251"
    )


def test_bootstrap_rejects_live_template_without_copying(tmp_path, monkeypatch):
    from render_web import setup_remote_avd as bootstrap

    monkeypatch.setattr(bootstrap.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        bootstrap.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=10**10)
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "check_output",
        lambda *a, **kw: "/sdk/qemu-system-x86_64 -avd firstlight-video251 -port 5582",
    )
    with pytest.raises(RuntimeError, match="Wait for AVD"):
        bootstrap.setup(
            dict(
                remote_avd="firstlight-render-web",
                remote_root=str(tmp_path / "workbench"),
            )
        )
    assert not (tmp_path / ".android/avd/firstlight-render-web.avd").exists()


def test_bootstrap_image_copy_is_independent_and_preserves_template(
    tmp_path, monkeypatch
):
    from render_web import setup_remote_avd as bootstrap

    monkeypatch.setattr(bootstrap.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        bootstrap.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=10**10)
    )
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *a, **kw: "")
    source = tmp_path / ".android/avd/firstlight-video251.avd"
    source.mkdir(parents=True)
    (source / "userdata-qemu.img.qcow2").write_bytes(b"verified-template")
    (source / "qemu-version.txt").write_text("2")
    (source / "version_num.cache").write_text("12960925\n")
    (source / "config.ini").write_text(
        "AvdId = firstlight-video251\nhw.ramSize = 24576\n"
    )
    cfg = dict(
        remote_avd="firstlight-render-web", remote_root=str(tmp_path / "workbench")
    )
    result = bootstrap.setup(cfg)
    assert result["avd"] == "firstlight-render-web"
    destination = source.with_name("firstlight-render-web.avd")
    (destination / "userdata-qemu.img.qcow2").write_bytes(b"new-game")
    assert (source / "userdata-qemu.img.qcow2").read_bytes() == b"verified-template"
    assert "firstlight-render-web" in (destination / "config.ini").read_text()
    assert "4096" in (destination / "config.ini").read_text()
    assert (destination / "qemu-version.txt").read_text() == "2"
    assert (destination / "version_num.cache").read_text() == "12960925\n"
    assert "path.rel=avd/firstlight-render-web.avd" in (
        destination.parent / "firstlight-render-web.ini"
    ).read_text()
    assert bootstrap.setup(cfg) == result
