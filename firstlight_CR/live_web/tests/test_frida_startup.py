import subprocess
import threading
from types import SimpleNamespace

import pytest

from live_web.device import Device


@pytest.fixture
def startup(monkeypatch):
    device = Device()
    stop = threading.Event()
    state = dict(running=False, now=0., calls=[], probes=0, ready_at=0., uid="0", log="", root_allowed=True)

    def adb(*args, **kwargs):
        state["calls"].append(args)
        if args == ("shell", "pidof", "cr-live-frida-server"):
            if not state["running"]:
                raise subprocess.CalledProcessError(1, args)
            return "42"
        if args == ("shell", "id", "-u"):
            return state["uid"]
        if args == ("root",):
            if state["root_allowed"]:
                state["uid"] = "0"
                return "restarting adbd as root"
            return "adbd cannot run as root in production builds"
        if args[0] == "shell" and args[1].startswith("nohup "):
            state["running"] = True
        if args == ("forward", "tcp:0", "tcp:27042"):
            return "39999"
        if args[:2] == ("shell", "tail"):
            return state["log"]
        return ""

    def ready(port):
        assert port == "39999"
        state["probes"] += 1
        return state["now"] >= state["ready_at"], "still initializing"

    def wait(seconds):
        state["now"] += seconds
        return stop.is_set()

    monkeypatch.setattr(device, "adb", adb)
    monkeypatch.setattr(device, "frida_ready", ready)
    monkeypatch.setattr(stop, "wait", wait)
    monkeypatch.setattr("live_web.device.time.monotonic", lambda: state["now"])
    return device, stop, state


def launches(state):
    return [call for call in state["calls"] if call[0] == "shell" and call[1].startswith("nohup ")]


def test_human_emulator_reuses_online_device_and_starts_installed_game(monkeypatch):
    device = Device()
    stop = threading.Event()
    adb_calls = []
    monkeypatch.setattr(device, "_adb_devices", staticmethod(lambda: {"emulator-5572": "device"}))
    monkeypatch.setattr(
        device,
        "adb_on",
        lambda serial, *args, **kwargs: adb_calls.append((serial, args)) or (
            "1" if args[:3] == ("shell", "getprop", "sys.boot_completed") else
            "package:/data/app/nullsroyale.rel.free/base.apk" if args[:3] == ("shell", "pm", "path") else ""
        ),
    )
    device.ensure_human_running(stop, timeout=2)
    assert adb_calls[-1] == (
        "emulator-5572",
        ("shell", "am", "start", "-n", device.game_component),
    )


def test_human_emulator_cold_start_uses_dedicated_avd(monkeypatch, tmp_path):
    device = Device()
    stop = threading.Event()
    state = {"devices": [{}, {"emulator-5572": "device"}], "now": 0.0, "popen": None}
    monkeypatch.setattr(device, "_adb_devices", staticmethod(lambda: state["devices"].pop(0)))
    monkeypatch.setattr("live_web.device.time.monotonic", lambda: state["now"])
    monkeypatch.setattr(stop, "wait", lambda seconds: state.__setitem__("now", state["now"] + seconds) or False)
    monkeypatch.setattr("live_web.device.subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    monkeypatch.setattr("live_web.device.subprocess.Popen", lambda command, **kwargs: state.__setitem__("popen", command))
    monkeypatch.setattr(
        device,
        "adb_on",
        lambda serial, *args, **kwargs: (
            "1" if args[:3] == ("shell", "getprop", "sys.boot_completed") else
            "package:/data/app/nullsroyale.rel.free/base.apk" if args[:3] == ("shell", "pm", "path") else ""
        ),
    )
    device.ensure_human_running(stop, timeout=3)
    assert state["popen"][state["popen"].index("-avd") + 1] == "firstlight-human"
    assert state["popen"][state["popen"].index("-port") + 1] == "5572"


def test_human_emulator_missing_game_is_reported(monkeypatch):
    device = Device()
    monkeypatch.setattr(device, "_adb_devices", staticmethod(lambda: {"emulator-5572": "device"}))
    monkeypatch.setattr(device, "adb_on", lambda *args, **kwargs: "1" if args[1:4] == ("shell", "getprop", "sys.boot_completed") else "")
    with pytest.raises(RuntimeError, match="未安装 nullsroyale.rel.free"):
        device.ensure_human_running(threading.Event(), timeout=2)


def test_human_emulator_stop_interrupts_wait(monkeypatch):
    device = Device()
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError, match="人类模拟器"):
        device.ensure_human_running(stop, timeout=2)


def test_live_emulator_waits_for_android_boot_even_when_adb_is_online(monkeypatch):
    device = Device()
    stop = threading.Event()
    state = dict(now=0.0, boot=iter(["0", "1"]), calls=[])

    def adb(*args, **kwargs):
        state["calls"].append(args)
        if args == ("get-state",):
            return "device"
        if args[:3] == ("shell", "getprop", "sys.boot_completed"):
            return next(state["boot"])
        raise AssertionError(args)

    monkeypatch.setattr(device, "adb", adb)
    monkeypatch.setattr(device, "ensure_frida", lambda stop: state.setdefault("frida", True))
    monkeypatch.setattr("live_web.device.time.monotonic", lambda: state["now"])
    monkeypatch.setattr(stop, "wait", lambda seconds: state.__setitem__("now", state["now"] + seconds) or False)

    device.ensure_running(stop)

    assert state["now"] == 2.0
    assert state["calls"].count(("shell", "getprop", "sys.boot_completed")) == 2
    assert state["frida"] is True


def test_live_emulator_offline_state_starts_or_reuses_avd_then_waits(monkeypatch):
    device = Device()
    stop = threading.Event()
    # One state check happens before the wait loop, then each loop iteration
    # checks ADB again before probing boot completion.
    state = dict(now=0.0, boot=iter(["0", "1"]),
                 states=iter(["offline", "offline", "device", "device"]), popen=None)

    def adb(*args, **kwargs):
        if args == ("get-state",):
            return next(state["states"])
        if args[:3] == ("shell", "getprop", "sys.boot_completed"):
            return next(state["boot"])
        raise AssertionError(args)

    monkeypatch.setattr(device, "adb", adb)
    monkeypatch.setattr(device, "_adb_devices", staticmethod(lambda: {device.serial: "offline"}))
    monkeypatch.setattr("live_web.device.subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    monkeypatch.setattr("live_web.device.subprocess.Popen", lambda command, **kwargs: state.__setitem__("popen", command))
    monkeypatch.setattr(device, "ensure_frida", lambda stop: None)
    monkeypatch.setattr("live_web.device.time.monotonic", lambda: state["now"])
    monkeypatch.setattr(stop, "wait", lambda seconds: state.__setitem__("now", state["now"] + seconds) or False)

    device.ensure_running(stop, frida=False)

    assert state["popen"][state["popen"].index("-avd") + 1] == "firstlight-live"
    assert state["popen"][state["popen"].index("-port") + 1] == "5570"
    assert state["now"] == 3.0


def test_cold_start_over_twelve_seconds_waits_for_real_readiness(startup):
    device, stop, state = startup
    state.update(ready_at=20., uid="2000")
    device.ensure_frida(stop)
    assert state["now"] == 20
    assert len(launches(state)) == 1
    command = launches(state)[0][1]
    assert " -D" not in command
    assert command.endswith("</dev/null >/data/local/tmp/cr-live-frida-server.log 2>&1 &")
    assert ("root",) in state["calls"]
    assert ("wait-for-device",) in state["calls"]
    assert state["calls"][-1] == ("forward", "--remove", "tcp:39999")


def test_existing_daemon_requires_api_readiness_and_is_not_restarted(startup):
    device, stop, state = startup
    state.update(running=True, ready_at=2.)
    device.ensure_frida(stop)
    assert not launches(state)
    assert state["probes"] == 5
    assert state["calls"][-1] == ("forward", "--remove", "tcp:39999")


def test_existing_daemon_still_prepares_privileged_adb_reads(startup):
    device, stop, state = startup
    state.update(running=True, uid="2000")
    device.ensure_frida(stop)
    assert state["uid"] == "0"
    assert ("wait-for-device",) in state["calls"]
    assert not launches(state)


def test_root_rejection_fails_before_waiting_or_starting_frida(startup):
    device, stop, state = startup
    state.update(uid="2000", root_allowed=False)
    with pytest.raises(RuntimeError, match="ADB.*root.*production builds"):
        device.wait_for_game_stable(stop)
    assert state["now"] == 0
    assert not launches(state)
    assert not any(call[:2] == ("shell", "pidof") for call in state["calls"])


@pytest.mark.parametrize("preparation", ["prepare_friendly", "home"])
def test_cold_game_preparation_roots_before_maps_and_attaches_after_stability(startup, monkeypatch, preparation):
    device, stop, state = startup
    state.update(uid="2000")
    native_adb = device.adb
    map_reads = []
    attach_times = []

    def adb(*args, **kwargs):
        if args == ("shell", "pidof", device.game_package):
            return "6162"
        if args == ("shell", "dumpsys", "activity", "activities"):
            return f"mResumedActivity: ActivityRecord{{u0 {device.game_component}}}"
        if args[:3] == ("shell", "grep", "-Fq"):
            assert state["uid"] == "0", "maps read before ADB root"
            map_reads.append(state["now"])
            return ""
        return native_adb(*args, **kwargs)

    monkeypatch.setattr(device, "adb", adb)
    monkeypatch.setattr(device, "ensure_running", lambda stop, *, frida: None)
    monkeypatch.setattr(device, "ensure_frida", lambda stop: attach_times.append(state["now"]))
    monkeypatch.setattr(device, "screen", lambda **kwargs: b"image")
    device.scene = "lobby"
    getattr(device, preparation)(stop)
    assert state["calls"].count(("root",)) == 1
    assert map_reads[0] == 0
    assert attach_times == [device.game_stability_seconds]
    assert device.stable_game_pid == 6162


def test_unresponsive_existing_server_times_out_without_duplicate(startup):
    device, stop, state = startup
    state.update(running=True, ready_at=100.)
    with pytest.raises(TimeoutError, match="45 秒内未就绪"):
        device.ensure_frida(stop)
    assert state["now"] == 45
    assert not launches(state)
    assert state["calls"][-1] == ("forward", "--remove", "tcp:39999")


def test_process_exit_reports_log_and_cleans_forward(startup, monkeypatch):
    device, stop, state = startup
    state["log"] = "Address already in use"
    def exited(port):
        state["running"] = False
        return False, "no response"
    monkeypatch.setattr(device, "frida_ready", exited)
    with pytest.raises(RuntimeError, match="Address already in use"):
        device.ensure_frida(stop)
    assert state["calls"][-1] == ("forward", "--remove", "tcp:39999")


def test_cancellation_before_start_does_not_touch_device(startup):
    device, stop, state = startup
    stop.set()
    with pytest.raises(InterruptedError):
        device.ensure_frida(stop)
    assert state["calls"] == []


def test_cancellation_during_readiness_cleans_forward(startup, monkeypatch):
    device, stop, state = startup
    def cancelled(port):
        stop.set()
        return True, ""
    monkeypatch.setattr(device, "frida_ready", cancelled)
    with pytest.raises(InterruptedError):
        device.ensure_frida(stop)
    assert state["calls"][-1] == ("forward", "--remove", "tcp:39999")


def test_game_stability_requires_one_continuous_healthy_pid(monkeypatch):
    device = Device()
    stop = threading.Event()
    state = dict(now=0., observations=iter([None, 101, 101, 202, 202, 202, 202, 202]))

    monkeypatch.setattr(device, "ensure_adb_root", lambda stop: None)
    monkeypatch.setattr(device, "game_observation", lambda: next(state["observations"]))
    monkeypatch.setattr("live_web.device.time.monotonic", lambda: state["now"])

    def wait(seconds):
        state["now"] += seconds
        return False

    monkeypatch.setattr(stop, "wait", wait)
    assert device.wait_for_game_stable(stop, settle_seconds=2, timeout=10) == 202
    assert state["now"] == 3.5
    assert device.stable_game_pid == 202


def test_game_observation_requires_resumed_game_and_mapped_native_library(monkeypatch):
    device = Device()
    calls = []

    def adb(*args, **kwargs):
        calls.append(args)
        if args == ("shell", "pidof", "nullsroyale.rel.free"):
            return "6162"
        if args == ("shell", "dumpsys", "activity", "activities"):
            return "  mResumedActivity: ActivityRecord{u0 nullsroyale.rel.free/com.supercell.clashroyale.GameApp}"
        if args == ("shell", "grep", "-Fq", "/libg.so", "/proc/6162/maps"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(device, "adb", adb)
    assert device.game_observation() == 6162
    assert calls[-1] == ("shell", "grep", "-Fq", "/libg.so", "/proc/6162/maps")


@pytest.mark.parametrize("detail", ["Permission denied", "Operation not permitted"])
def test_maps_permission_error_is_reported_instead_of_game_stability_timeout(monkeypatch, detail):
    device = Device()

    def adb(*args, **kwargs):
        if args == ("shell", "pidof", device.game_package):
            return "6162"
        if args == ("shell", "dumpsys", "activity", "activities"):
            return f"mResumedActivity: ActivityRecord{{u0 {device.game_component}}}"
        raise subprocess.CalledProcessError(2, args, output=f"grep: /proc/6162/maps: {detail}")

    monkeypatch.setattr(device, "adb", adb)
    with pytest.raises(RuntimeError, match="游戏状态读取权限不足.*" + detail):
        device.game_observation()


@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
def test_readiness_uses_live_python_and_bounded_api_call(monkeypatch, outcome):
    def run(command, **kwargs):
        assert command[0].endswith(".venv-live/bin/python")
        assert "enumerate_processes()" in command[2]
        assert command[3] == "39999"
        assert kwargs["timeout"] == 3
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 3)
        return SimpleNamespace(returncode=int(outcome == "error"), stderr="Traceback\nconnection failed")
    monkeypatch.setattr("live_web.device.subprocess.run", run)
    ready, detail = Device().frida_ready("39999")
    assert ready == (outcome == "success")
    assert detail
