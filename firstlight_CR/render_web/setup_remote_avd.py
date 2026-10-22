"""Provision an independent AVD by copying a verified template while it is stopped."""

import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

if __package__:
    from .remote_agent import avd_process_running, write
else:
    from remote_agent import avd_process_running, write


def setup(cfg):
    name = cfg["remote_avd"]
    if name != "firstlight-render-web":
        raise ValueError("bootstrap only provisions firstlight-render-web")
    root = Path(cfg["remote_root"])
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / "bootstrap.json"
    previous = json.loads(receipt.read_text()) if receipt.exists() else {}
    avds = Path.home() / ".android/avd"
    source = avds / "firstlight-video251.avd"
    destination = avds / (name + ".avd")
    ini = avds / (name + ".ini")
    if (
        previous.get("ready")
        and previous.get("avd") == name
        and previous.get("method") == "cold-avd-copy.v2"
        and destination.exists()
    ):
        return previous
    if (destination.exists() or ini.exists()) and previous.get("avd") != name:
        raise RuntimeError("Existing AVD has no workbench ownership receipt")
    if shutil.disk_usage(root).free < 8_000_000_000:
        raise RuntimeError("Provisioning requires 8 GB free")

    def check_idle():
        processes = subprocess.check_output(["ps", "-eo", "args="], text=True)
        for avd in ("firstlight-video251", name):
            if avd_process_running(processes, avd):
                raise RuntimeError("Wait for AVD to stop before copying: " + avd)

    with (root / "engine.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_idle()
        stage = avds / (name + ".staging-" + uuid.uuid4().hex)
        stage.mkdir()
        images = sorted(set(source.glob("*.img")) | set(source.glob("*.qcow2")))
        if not any(p.name == "userdata-qemu.img.qcow2" for p in images):
            raise RuntimeError("Verified template userdata image missing")
        sizes = {}
        for path in images:
            shutil.copy2(path, stage / path.name)
            sizes[path.name] = path.stat().st_size
        # Without the QEMU version marker, emulator treats existing qcow2 files
        # as belonging to an older backend and silently recreates their contents.
        for marker in ("qemu-version.txt", "version_num.cache"):
            shutil.copy2(source / marker, stage / marker)
        check_idle()
        config = (
            (source / "config.ini").read_text().replace("firstlight-video251", name)
        )
        config = config.replace("hw.ramSize = 24576", "hw.ramSize = 4096")
        (stage / "config.ini").write_text(config)
        if destination.exists():
            backups = root / "bootstrap-backups"
            backups.mkdir(exist_ok=True)
            destination.rename(backups / (name + "-" + uuid.uuid4().hex))
        stage.rename(destination)
        ini.write_text(
            f"avd.ini.encoding=UTF-8\npath={destination}\npath.rel=avd/{name}.avd\ntarget=android-35\n"
        )
        data = dict(
            ready=True,
            avd=name,
            source=str(source),
            method="cold-avd-copy.v2",
            copied_images=sizes,
            created=time.time(),
        )
        write(receipt, data)
        return data
