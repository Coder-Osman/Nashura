import copy
import errno
import json
import os
import struct
from pathlib import Path

import pytest

import apex_loop_devices as probe


@pytest.fixture
def devices(tmp_path, monkeypatch):
    sysfs, packages = tmp_path / "sys", tmp_path / "packages"
    sysfs.mkdir()
    packages.mkdir()
    control = tmp_path / "control"
    control.touch()
    state = {"loop0": {"dev": "7:0", "loop/backing_file": "/snap/existing.snap"}}
    calls = []

    def ioctl(fd, action, number=None):
        calls.append((action, number))
        if action == probe.LOOP_CTL_ADD:
            state[f"loop{number}"] = {"dev": f"7:{number}"}
            path = sysfs / f"loop{number}"
            path.mkdir()
            (path / "dev").write_text(f"7:{number}")
        elif action == probe.LOOP_CTL_REMOVE:
            del state[f"loop{number}"]
        elif action == probe.LOOP_CLR_FD:
            state["loop2"].pop("loop/backing_file")
        elif action == probe.LOOP_GET_STATUS64:
            path = packages / "original.apex"
            identity = (path.stat().st_dev, path.stat().st_ino) if path.exists() else (0, 0)
            # Only the loop2 file descriptor belongs to our package in this test.
            if os.fstat(fd).st_ino != (tmp_path / "output/block/loop2").stat().st_ino:
                identity = (0, 0)
            return struct.pack("=QQ", *identity) + bytes(216)

    monkeypatch.setattr(probe, "SYS_BLOCK", sysfs)
    monkeypatch.setattr(probe, "LOOP_CONTROL", control)
    monkeypatch.setattr(probe, "snapshot", lambda: copy.deepcopy(state))
    monkeypatch.setattr(probe.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(probe.os, "mknod", lambda path, *_: Path(path).touch())
    return packages, state, calls, ioctl


def test_occupied_device_not_exposed_and_idle_device_preserved(tmp_path, devices):
    packages, state, calls, _ = devices
    (packages / "original.apex").touch()
    state["loop1"] = {"dev": "7:1"}
    path = probe.SYS_BLOCK / "loop1"
    path.mkdir()
    (path / "dev").write_text("7:1")
    with probe.ApexLoopDevices(tmp_path / "output", packages) as output:
        assert (output / "block/loop1").exists()
        assert not (output / "block/loop0").exists()
    assert set(state) == {"loop0", "loop1"}
    assert not any(action == probe.LOOP_CTL_REMOVE and number in (0, 1) for action, number in calls)


def test_partial_allocation_failure_cleans_only_created_devices(tmp_path, devices, monkeypatch):
    packages, state, calls, ioctl = devices

    def fail(fd, action, number=None):
        if action == probe.LOOP_CTL_ADD and number == 4:
            raise OSError(errno.ENOSPC, "controlled allocation failure")
        return ioctl(fd, action, number)

    monkeypatch.setattr(probe.fcntl, "ioctl", fail)
    with pytest.raises(OSError, match="allocation failure"):
        with probe.ApexLoopDevices(tmp_path / "output", packages):
            pass
    assert list(state) == ["loop0"]
    assert [number for action, number in calls if action == probe.LOOP_CTL_REMOVE] == [3, 2, 1]
    result = json.loads((tmp_path / "output/result.json").read_text())
    assert result["preexisting_devices_unchanged"]
    assert result["created_devices_removed"]


def test_unexpected_owner_preserved_while_own_apex_is_detached(tmp_path, devices):
    packages, state, calls, _ = devices
    (packages / "original.apex").touch()
    with pytest.raises(RuntimeError, match="cleanup requires inspection"):
        with probe.ApexLoopDevices(tmp_path / "output", packages):
            state["loop1"]["loop/backing_file"] = "/snap/new-owner.snap"
            state["loop2"]["loop/backing_file"] = "/original.apex"
    assert state["loop1"]["loop/backing_file"] == "/snap/new-owner.snap"
    assert [number for action, number in calls if action == probe.LOOP_CTL_REMOVE][-1] == 2
    assert sum(action == probe.LOOP_CLR_FD for action, _ in calls) == 1
    result = json.loads((tmp_path / "output/result.json").read_text())
    assert result["cleanup_errors"][0]["device"] == "loop1"
    assert result["preexisting_devices_unchanged"]
