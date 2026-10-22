import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from live_web.app import create_app
from live_web.device import Device
from live_web.service import ASSETS, BATTLE_CAPTURE_SECONDS, BATTLE_POLICY_SECONDS, JsonlTail, Service
from native_runner.live.control import device_lease


class FakeCatalog:
    row = dict(id="chosen", label="RL chosen", name="chosen.pt", path="/tmp/chosen.pt", supported=True)

    def get(self, identity):
        if identity != "chosen":
            raise ValueError("Checkpoint not found")
        return dict(self.row)

    def items(self):
        return [dict(self.row)]

    def add(self, path):
        raise ValueError("Invalid checkpoint")

    def start(self):
        pass

    def close(self):
        pass

    def status(self):
        return dict(refreshing=False, error=None)

    def refresh_async(self):
        pass

    def prepare(self, row, **kwargs):
        return row


@pytest.fixture
def service(tmp_path):
    instance = Service(data=tmp_path, catalog=FakeCatalog())
    instance.device.serial = "test-live-dashboard-service"
    return instance


def test_jsonl_reader_keeps_partial_line(tmp_path):
    path = tmp_path / "events.jsonl"
    tail = JsonlTail(path)
    assert tail.read() == []
    path.write_bytes(b'{"event":"ready"}\n{"event":')
    assert tail.read() == [{"event": "ready"}]
    assert tail.read() == []
    with path.open("ab") as out:
        out.write(b'"bound"}\n')
    assert tail.read() == [{"event": "bound"}]


def test_native_capture_reason_reaches_dashboard_warning(service, tmp_path):
    from native_runner.live.snapshot import LiveStateError, latest_snapshot
    reason = "Error: invalid HP: entity=5000060 name=TombstoneHero_visual_dummy max_hp=-1"
    capture = tmp_path / "capture.jsonl"
    capture.write_text(json.dumps({"event": "read_error", "message": reason}) + "\n")
    with pytest.raises(LiveStateError) as caught:
        latest_snapshot(capture)
    service.current = dict(id="capture-error", state="playing", events=[], warning=None, created_at=0)
    (service.data / "jobs" / "capture-error").mkdir(parents=True)
    service.policy_event(dict(event="blocked", reason=str(caught.value)))
    assert reason in service.status()["job"]["warning"]
    assert reason in service.history()[0]["warning"]


def test_device_lease_prevents_competing_runner():
    with device_lease("test-live-dashboard"):
        with pytest.raises(ValueError, match="already controls"):
            with device_lease("test-live-dashboard"):
                pass
    with device_lease("test-live-dashboard"):
        pass


@pytest.mark.parametrize("scene,file", [
    ("lobby", "max-03-ready.png"), ("result", "max-03-result.png"),
    ("battle_log", "window-before.png"),
])
def test_navigation_recognizes_known_screens(scene, file):
    assert Device().classify(Image.open(ASSETS / file)) == scene


def test_navigation_rejects_unknown_and_wrong_resolution():
    device = Device()
    assert device.classify(Image.new("RGB", (1080, 1920))) == "unknown"
    with pytest.raises(ValueError, match="viewport"):
        device.classify(Image.new("RGB", (540, 960)))


def test_cancel_before_matching_never_taps(monkeypatch):
    device = Device()
    device.scene = "lobby"
    monkeypatch.setattr(device, "screen", lambda **kw: b"")
    monkeypatch.setattr(device, "tap", lambda *args: pytest.fail("Unexpected tap"))
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        device.match(stop)


def test_wrong_screen_never_taps(monkeypatch):
    device = Device()
    monkeypatch.setattr(device, "screen", lambda **kw: b"")
    monkeypatch.setattr(device, "tap", lambda *args: pytest.fail("Unexpected tap"))
    with pytest.raises(ValueError, match="lobby"):
        device.match(threading.Event())


def test_duplicate_start_and_stop(service, monkeypatch):
    def work(*args):
        service.stop_event.wait(3)
        service.update(state="stopped")

    monkeypatch.setattr(service, "work", work)
    service.start("chosen")
    try:
        with pytest.raises(RuntimeError):
            service.start("chosen")
        assert service.status()["busy"]
        service.stop()
        service.worker.join(2)
        assert not service.status()["busy"]
        assert service.history()[0]["state"] == "stopped"
    finally:
        service.close()


def test_startup_failure_is_saved(service, monkeypatch):
    def failed(*args):
        raise ValueError("fixture device disconnected")

    monkeypatch.setattr(service.device, "home", failed)
    service.start("chosen")
    service.worker.join(2)
    assert service.current["state"] == "failed"
    assert "disconnected" in service.current["message"]
    assert not service.status()["busy"]
    assert service.history()[0]["ended_at"] >= service.current["created_at"]


def test_restart_marks_unfinished_history(service):
    path = service.data / "jobs" / "unfinished"
    path.mkdir()
    (path / "summary.json").write_text(json.dumps(dict(id="unfinished", state="playing", created_at=0)))
    service.open()
    try:
        assert service.history()[0]["state"] == "interrupted"
    finally:
        service.close()


def test_api_origin_and_unknown_checkpoint(service):
    with TestClient(create_app(service)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["app"] == "cr-live-web"
        body = {"checkpoint_id": "missing"}
        assert client.post("/api/start", json=body).status_code == 403
        headers = {"X-Live-Control": "1", "Origin": "http://other-site.example"}
        assert client.post("/api/start", json=body, headers=headers).status_code == 403
        headers = {"X-Live-Control": "1", "Origin": "http://testserver"}
        assert client.post("/api/start", json=body, headers=headers).status_code == 422
        assert client.post("/api/start", json={"checkpoint_id": "chosen", "execute": False}, headers=headers).status_code == 422
        assert client.get("/api/status").json()["busy"] is False
        assert client.get("/api/jobs/unknown/files/summary.json").status_code == 404


def test_ready_cancellation_does_not_wait_for_loading(service, tmp_path):
    service.stop_event.set()
    with pytest.raises(InterruptedError):
        service.ready(None, tmp_path / "absent.jsonl")


@pytest.mark.parametrize("stop_after_load", [False, True])
@pytest.mark.parametrize("terminal_pending", [False, True])
def test_match_lifecycle_cleans_both_children(service, monkeypatch, stop_after_load, terminal_pending):
    from types import SimpleNamespace

    stopped = []
    tapped = []
    spawned = []
    monkeypatch.setattr(service.device, "home", lambda stop: None)
    monkeypatch.setattr(service.device, "match", lambda stop: tapped.append(True))
    monkeypatch.setattr(service.device, "screen", lambda **kwargs: b"image")
    service.device.png = b"image"
    service.device.scene = "result"
    monkeypatch.setattr(service, "shutdown", lambda process: stopped.append(process.name) if process else None)
    original_wait = service.stop_event.wait
    monkeypatch.setattr(service.stop_event, "wait", lambda seconds: original_wait(0) if seconds == 3 else original_wait(seconds))

    def spawn(module, arguments, log):
        spawned.append((module, list(arguments)))
        output = Path(arguments[arguments.index("--output") + 1])
        events = [{"event": "ready"}]
        if module.endswith(".run"):
            assert arguments[arguments.index("--checkpoint") + 1] == "/tmp/chosen.pt"
            events += [{"event": "bound", "tick": 150},
                       {"event": "confirmed", "tick": 200, "card_id": 27000009}]
            if terminal_pending:
                events.append({"event": "unconfirmed_at_terminal", "tick": 300, "card_id": 28000011})
            events += [{"event": "terminal", "tick": 300, "same_battle": True,
                        "winner": 0, "own_side": 0,
                        "counts": dict(decisions=2, submitted=2 if terminal_pending else 1,
                                       confirmed=1, cancelled=0)}]
            if stop_after_load:
                service.stop_event.set()
        output.write_text("".join(json.dumps(e) + "\n" for e in events))
        return SimpleNamespace(name=module, poll=lambda: None)

    monkeypatch.setattr(service, "spawn", spawn)
    service.start("chosen")
    service.worker.join(3)
    assert not service.status()["busy"]
    assert stopped == ["native_runner.live.run", "native_runner.live.capture"]
    assert service.current["state"] == ("stopped" if stop_after_load else "completed")
    assert tapped == ([] if stop_after_load else [True])
    if not stop_after_load:
        assert service.current["result"] == "win"
        assert service.current["counts"]["confirmed"] == 1
        assert service.current["counts"]["submitted"] == (2 if terminal_pending else 1)
    capture_args = next(args for module, args in spawned if module.endswith(".capture"))
    policy_args = next(args for module, args in spawned if module.endswith(".run"))
    assert capture_args[capture_args.index("--seconds") + 1] == BATTLE_CAPTURE_SECONDS
    assert policy_args[policy_args.index("--seconds") + 1] == BATTLE_POLICY_SECONDS


def test_friendly_mode_waits_without_tapping_battle(service, monkeypatch):
    from types import SimpleNamespace

    spawned = []
    service.match_mode = "friendly"
    monkeypatch.setattr(service.device, "prepare_friendly", lambda stop: None)
    monkeypatch.setattr(service.device, "home", lambda stop: pytest.fail("friendly mode navigated home"))
    monkeypatch.setattr(service.device, "match", lambda stop: pytest.fail("friendly mode tapped Battle"))
    monkeypatch.setattr(service.device, "screen", lambda **kwargs: b"image")
    service.device.png = b"image"
    service.device.scene = "result"
    monkeypatch.setattr(service, "shutdown", lambda process: None)
    original_wait = service.stop_event.wait
    monkeypatch.setattr(service.stop_event, "wait", lambda seconds: original_wait(0) if seconds == 3 else original_wait(seconds))

    def spawn(module, arguments, log):
        spawned.append((module, list(arguments)))
        output = Path(arguments[arguments.index("--output") + 1])
        events = [{"event": "ready"}]
        output.write_text("".join(json.dumps(e) + "\n" for e in events))
        return SimpleNamespace(name=module, poll=lambda: None)

    monkeypatch.setattr(service, "spawn", spawn)
    job = service.start("chosen")
    deadline = time.monotonic() + 3
    while service.current["state"] != "waiting_battle" and time.monotonic() < deadline:
        time.sleep(.05)
    assert service.current["state"] == "waiting_battle"
    assert service.current["purpose"] == "interactive_test"
    assert service.current["training_export"] is False
    assert service.status()["busy"]
    policy_path = service.data / "jobs" / job["id"] / "policy.jsonl"
    with policy_path.open("a") as output:
        for event in [{"event": "bound", "tick": 150, "own_side": 1},
                      {"event": "terminal", "tick": 300, "same_battle": True,
                       "winner": -1, "own_side": 1,
                       "counts": dict(decisions=0, submitted=0, confirmed=0, cancelled=0)}]:
            output.write(json.dumps(event) + "\n")
    service.worker.join(3)
    assert not service.status()["busy"]
    assert service.current["state"] == "completed"
    assert service.current["result"] == "draw"
    assert service.current["mode"] == "friendly"
    capture_args = next(args for module, args in spawned if module.endswith(".capture"))
    policy_args = next(args for module, args in spawned if module.endswith(".run"))
    assert capture_args[capture_args.index("--seconds") + 1] == BATTLE_CAPTURE_SECONDS
    assert policy_args[policy_args.index("--seconds") + 1] == BATTLE_POLICY_SECONDS


def test_friendly_preparation_preserves_invitation_screen(monkeypatch):
    device = Device()
    calls = []
    monkeypatch.setattr(device, "ensure_running", lambda stop, *, frida: calls.append(("running", frida)))
    monkeypatch.setattr(device, "adb", lambda *args: calls.append(args))
    monkeypatch.setattr(device, "wait_for_game_stable", lambda stop: calls.append(("stable",)))
    monkeypatch.setattr(device, "ensure_frida", lambda stop: calls.append(("frida",)))
    monkeypatch.setattr(device, "screen", lambda **kwargs: b"image")
    monkeypatch.setattr(device, "tap", lambda *args: pytest.fail("Unexpected navigation tap"))
    device.prepare_friendly(threading.Event())
    assert calls == [
        ("running", False),
        ("shell", "am", "start", "-n", "nullsroyale.rel.free/com.supercell.clashroyale.GameApp"),
        ("stable",),
        ("frida",),
    ]


def test_invalid_mode_rejected_before_start(service):
    with pytest.raises(ValueError, match="模式"):
        service.start("chosen", match_mode="invalid")
    assert service.current is None
