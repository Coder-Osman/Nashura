import json
import subprocess

import pytest

from render_web import local_render, runner
from render_web.config import Config, write_json


@pytest.fixture
def recording(tmp_path, monkeypatch):
    config = Config(data=tmp_path)
    monkeypatch.setattr(local_render, "validate_replay", lambda *args: (
        {}, {"ending_wall_s": 153.5},
    ))
    monkeypatch.setattr(local_render.subprocess, "check_output", lambda *args, **kw:
                        json.dumps({"streams": [{"width": 1080, "height": 1920}],
                                    "format": {"duration": "173.69"}}))
    return config, tmp_path


@pytest.mark.parametrize("exitcode", [0, 1, 139, None])
def test_recording_requires_decode_even_when_exit_status_is_missing(recording, monkeypatch, exitcode):
    config, root = recording
    if exitcode is not None:
        write_json(root / "recording-exit.json", {"returncode": exitcode})
    calls = []
    monkeypatch.setattr(local_render, "run_export_process", lambda *args: calls.append(args))
    result = local_render.validate_recording(config, root, {}, lambda: False)
    assert result["full_frame_decode"] == "passed"
    assert len(calls) == 1 and "-xerror" in calls[0][1]
    assert result["recorder_exit"] == (None if exitcode is None else {"returncode": exitcode})


@pytest.mark.parametrize("failure", ["decode", "duration", "replay"])
def test_exit_warning_never_bypasses_validation(recording, monkeypatch, failure):
    config, root = recording
    write_json(root / "recording-exit.json", {"returncode": 1})
    if failure == "duration":
        monkeypatch.setattr(local_render.subprocess, "check_output", lambda *a, **kw:
                            '{"streams":[{"width":1080,"height":1920}],"format":{"duration":"150"}}')
    elif failure == "replay":
        def invalid(*args):
            raise ValueError("wrong winner")
        monkeypatch.setattr(local_render, "validate_replay", invalid)
    def corrupt(*args):
        raise RuntimeError("decode failed")
    monkeypatch.setattr(local_render, "run_export_process", corrupt)
    with pytest.raises((ValueError, RuntimeError)):
        local_render.validate_recording(config, root, {}, lambda: False)
    assert not (root / "recording-validation.json").exists()


def test_cancel_prevents_recording_acceptance(recording):
    config, root = recording
    with pytest.raises(local_render.Cancelled):
        local_render.validate_recording(config, root, {}, lambda: True)


def test_boot_timeout_diagnoses_and_closes_before_one_retry(tmp_path, monkeypatch):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    events = []
    def boot(attempt):
        events.append(("boot", attempt))
        if attempt == 1:
            raise TimeoutError("boot")
    monkeypatch.setattr(renderer, "boot_once", boot)
    monkeypatch.setattr(renderer, "boot_diagnostics", lambda a: events.append(("diagnose", a)))
    monkeypatch.setattr(renderer, "close", lambda: events.append(("close", 1)))
    renderer.boot()
    assert events == [("boot", 1), ("diagnose", 1), ("close", 1), ("boot", 2)]


def test_boot_stops_after_second_timeout(tmp_path, monkeypatch):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    attempts = []
    def boot(attempt):
        attempts.append(attempt)
        raise TimeoutError("boot")
    monkeypatch.setattr(renderer, "boot_once", boot)
    monkeypatch.setattr(renderer, "boot_diagnostics", lambda a: None)
    monkeypatch.setattr(renderer, "close", lambda: None)
    with pytest.raises(TimeoutError, match="boot-diagnostics-2.json"):
        renderer.boot()
    assert attempts == [1, 2]


def test_boot_never_retries_with_previous_emulator_alive(tmp_path, monkeypatch):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    attempts = []
    def boot(attempt):
        attempts.append(attempt)
        raise TimeoutError("boot")
    monkeypatch.setattr(renderer, "boot_once", boot)
    monkeypatch.setattr(renderer, "boot_diagnostics", lambda a: None)
    monkeypatch.setattr(renderer, "close", lambda: None)
    monkeypatch.setattr(local_render, "live", lambda owner: True)
    with pytest.raises(RuntimeError, match="已停止重试"):
        renderer.boot()
    assert attempts == [1]


@pytest.mark.parametrize("exc", [local_render.Cancelled("cancel"), RuntimeError("emulator exited")])
def test_boot_does_not_retry_cancel_or_unrelated_error(tmp_path, monkeypatch, exc):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    def boot(attempt):
        raise exc
    monkeypatch.setattr(renderer, "boot_once", boot)
    monkeypatch.setattr(renderer, "boot_diagnostics", lambda a: pytest.fail("unexpected retry"))
    with pytest.raises(type(exc)):
        renderer.boot()


def test_boot_diagnostics_preserves_adb_errors_and_timeouts(tmp_path, monkeypatch):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    def run(args, **kwargs):
        if "logcat" in args:
            raise subprocess.TimeoutExpired(args, 5)
        return subprocess.CompletedProcess(args, 1, "", "device offline")
    monkeypatch.setattr(local_render.subprocess, "run", run)
    renderer.boot_diagnostics(1)
    data = json.loads((tmp_path / "boot-diagnostics-1.json").read_text())
    assert data["probes"]["state"]["stderr"] == "device offline"
    assert "error" in data["probes"]["logcat"]


@pytest.mark.parametrize("blocks,raises", [(0, True), (1000, True), (200000, False)])
def test_guest_space_guard_checks_android_partition(tmp_path, monkeypatch, blocks, raises):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    monkeypatch.setattr(renderer, "adb", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, f"{blocks}:4096", ""))
    if raises:
        with pytest.raises(local_render.GuestStorageError, match="不是 Mac 磁盘"):
            renderer.check_guest_space(600 * 2**20)
    else:
        renderer.check_guest_space(600 * 2**20)
    assert json.loads((tmp_path / "guest-disk.json").read_text())["free_bytes"] == blocks * 4096


def test_full_guest_disk_does_not_trigger_useless_boot_retry(tmp_path, monkeypatch):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    calls = []
    def boot(attempt):
        calls.append(attempt)
        raise local_render.GuestStorageError("full")
    monkeypatch.setattr(renderer, "boot_once", boot)
    monkeypatch.setattr(renderer, "boot_diagnostics", lambda a: None)
    monkeypatch.setattr(renderer, "close", lambda: None)
    with pytest.raises(local_render.GuestStorageError):
        renderer.boot()
    assert calls == [1]


@pytest.mark.parametrize("focus,blocked", [
    ("nullsroyale.rel.free/Engine0App", False),
    ("Application Error: com.google.android.gms", True),
    ("Application Not Responding: nullsroyale.rel.free", True),
])
def test_crash_dialog_blocks_publication(tmp_path, monkeypatch, focus, blocked):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    monkeypatch.setattr(renderer, "adb", lambda *a, **kw: subprocess.CompletedProcess(
        a, 0, "mCurrentFocus=Window{u0 " + focus + "}", "",
    ))
    if blocked:
        with pytest.raises(RuntimeError, match="弹窗遮挡"):
            renderer.check_crash_dialog()
        assert not json.loads((tmp_path / "visual-review.json").read_text())["passed"]
    else:
        renderer.check_crash_dialog()


def test_visual_rejection_prevents_reusing_a_decodable_recording(tmp_path, monkeypatch):
    previous = tmp_path / "previous"
    previous.mkdir()
    root = tmp_path / "retry"
    root.mkdir()
    write_json(previous / "visual-review.json", {"passed": False})
    monkeypatch.setattr(runner, "validate_replay", lambda *a: pytest.fail("rejected video reused"))
    assert not runner.reuse_recording(Config(), previous, root, {}, lambda: False)


@pytest.mark.parametrize("valid", [True, False])
def test_retry_rechecks_raw_video_without_copying_process_ownership(tmp_path, monkeypatch, valid):
    previous = tmp_path / "previous"
    root = tmp_path / "retry"
    previous.mkdir()
    root.mkdir()
    (previous / "native-full.mp4").write_bytes(b"video fixture")
    write_json(previous / "emulator-owner.json", {"pid": 42})
    monkeypatch.setattr(runner, "validate_replay", lambda *args: None)
    def validate(config, candidate, request, cancelled):
        assert (candidate / "native-full.mp4").read_bytes() == b"video fixture"
        if not valid:
            raise RuntimeError("bad video")
    monkeypatch.setattr(runner, "validate_recording", validate)
    assert runner.reuse_recording(Config(), previous, root, {}, lambda: False) is valid
    assert not (root / "emulator-owner.json").exists()
    assert json.loads((root / "recording-reuse.json").read_text())["reused"] is valid
