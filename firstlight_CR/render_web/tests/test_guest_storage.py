import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from render_web.config import Config
from render_web.guest_storage import cleanup_guest_recordings
from render_web import local_render


JOB = "a" * 32
NAME = f"render-web-{JOB}.mp4"
REMOTE = "/data/media/0/" + NAME
VIDEO = b"complete guest recording"
DIGEST = hashlib.sha256(VIDEO).hexdigest()


class Guest:
    def __init__(self):
        self.calls = []
        self.paths = [REMOTE]
        self.recording = False
        self.pull_error = False
        self.bad_copy = False
        self.delete_error = False
        self.removed = False

    def adb(self, *args, **kwargs):
        self.calls.append(args)
        code, out, err = 0, "", ""
        if args == ("shell", "pidof", "screenrecord"):
            code, out = (0, "42") if self.recording else (1, "")
        elif args[:2] == ("shell", "stat"):
            out = f"{300000 if self.removed else 100000}:4096"
        elif args[0] == "shell" and args[1].startswith("su 0 find"):
            out = "\n".join(self.paths)
        elif args[:4] == ("shell", "su", "0", "sha256sum"):
            out = DIGEST + "  " + args[4]
        elif args[:4] == ("shell", "su", "0", "stat"):
            out = str(len(VIDEO))
        elif args[0] == "pull":
            if self.pull_error:
                code, err = 1, "connection lost"
            else:
                Path(args[2]).write_bytes(b"truncated" if self.bad_copy else VIDEO)
        elif args[:4] == ("shell", "su", "0", "rm"):
            if self.delete_error:
                code, err = 1, "permission denied"
            else:
                self.removed = True
        elif args[:4] == ("shell", "su", "0", "test"):
            code = 0 if self.removed else 1
        elif args == ("shell", "su", "0", "sync"):
            pass
        else:
            pytest.fail(f"unexpected guest command: {args}")
        return subprocess.CompletedProcess(args, code, out, err)


def test_verified_copy_removed_via_backing_path(tmp_path):
    host = tmp_path / "jobs" / JOB / "native-full.mp4"
    host.parent.mkdir(parents=True)
    host.write_bytes(VIDEO)
    guest = Guest()
    # Even misleading filenames and unrelated recordings must be preserved.
    guest.paths += ["/data/media/0/personal.mp4", "/data/media/0/render-web-unknown.mp4",
                    "/data/media/0/../" + NAME]
    report = cleanup_guest_recordings(Config(data=tmp_path), tmp_path, guest.adb, phase="startup")
    assert len(report["files"]) == 1 and report["files"][0]["removed"]
    assert report["reclaimed_bytes"] > 0 and not report["errors"]
    assert report["synced"]
    assert not any(c[0] == "pull" for c in guest.calls)
    assert ("shell", "su", "0", "rm", "-f", REMOTE) in guest.calls
    assert guest.calls.index(("shell", "su", "0", "sync")) > guest.calls.index(
        ("shell", "su", "0", "rm", "-f", REMOTE))
    assert host.read_bytes() == VIDEO


@pytest.mark.parametrize("existing", [False, True])
def test_interrupted_recording_archived_without_overwriting_host(tmp_path, existing):
    host = tmp_path / "jobs" / JOB / "native-full.mp4"
    if existing:
        host.parent.mkdir(parents=True)
        host.write_bytes(b"different host recording")
    guest = Guest()
    report = cleanup_guest_recordings(Config(data=tmp_path), tmp_path, guest.adb, phase="recovery")
    item = report["files"][0]
    assert item["removed"] and Path(item["host"]).read_bytes() == VIDEO
    assert Path(item["host"]).parent == tmp_path / "guest-recording-archive"
    if existing:
        assert host.read_bytes() == b"different host recording"
    else:
        assert not host.exists()
    assert json.loads((tmp_path / "guest-cleanup-recovery.json").read_text()) == report


@pytest.mark.parametrize("failure", ["pull_error", "bad_copy", "delete_error", "host_full"])
def test_failed_cleanup_preserves_original_and_reports_failure(tmp_path, failure):
    guest = Guest()
    setattr(guest, failure, True)
    config = Config(data=tmp_path, min_free_bytes=2**63 if failure == "host_full" else 0)
    report = cleanup_guest_recordings(config, tmp_path, guest.adb, phase="shutdown")
    assert not guest.removed and report["errors"]
    assert not report["files"][0]["removed"]
    if failure != "delete_error":
        assert not any(c[:4] == ("shell", "su", "0", "rm") for c in guest.calls)


def test_active_screenrecord_prevents_any_cleanup(tmp_path):
    guest = Guest()
    guest.recording = True
    report = cleanup_guest_recordings(Config(data=tmp_path), tmp_path, guest.adb, phase="shutdown")
    assert report["deferred"] and not report["files"]
    assert guest.calls == [("shell", "pidof", "screenrecord")]


@pytest.mark.parametrize("alive,serial", [(False, "emulator-5554"), (True, "emulator-5580")])
def test_cleanup_requires_matching_owned_guest(tmp_path, monkeypatch, alive, serial):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    renderer.owner = dict(pid=1, birth="owned", serial=serial)
    monkeypatch.setattr(local_render, "live", lambda owner: alive)
    monkeypatch.setattr(renderer, "adb", lambda *a, **kw: pytest.fail("unowned guest accessed"))
    renderer.cleanup_guest_storage("shutdown")


def test_boot_reclaims_recordings_before_space_guard(tmp_path, monkeypatch):
    renderer = local_render.Renderer(Config(), tmp_path, {}, lambda *a, **kw: None, lambda: False)
    monkeypatch.setattr(local_render.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "", ""))
    class Emulator:
        pid = 123
        def poll(self):
            return None
    monkeypatch.setattr(local_render.subprocess, "Popen", lambda *a, **kw: Emulator())
    monkeypatch.setattr(local_render, "birth", lambda pid: "owned")
    monkeypatch.setattr(renderer, "adb", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "1", ""))
    calls = []
    monkeypatch.setattr(renderer, "cleanup_guest_storage", lambda phase: calls.append(phase))
    def space(minimum, **kwargs):
        if minimum == 600 * 2**20:
            assert "startup" in calls
        calls.append(minimum)
    monkeypatch.setattr(renderer, "check_guest_space", space)
    renderer.boot_once(1)
    assert calls == [256 * 2**20, "startup", 600 * 2**20]
