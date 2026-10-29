"""Provide original apexd with new loop devices and clean up only those devices."""
import fcntl
import json
import os
from pathlib import Path
import stat
import struct

SYS_BLOCK = Path("/sys/block")
LOOP_CONTROL = Path("/dev/loop-control")
LOOP_CTL_ADD, LOOP_CTL_REMOVE, LOOP_CLR_FD = 0x4C80, 0x4C81, 0x4C01
LOOP_GET_STATUS64 = 0x4C05


def snapshot():
    fields = ("dev", "queue/read_ahead_kb", "loop/backing_file", "loop/offset",
              "loop/sizelimit", "loop/autoclear")
    return {path.name: {name: (path / name).read_text().strip()
                        for name in fields if (path / name).exists()}
            for path in SYS_BLOCK.glob("loop*")}


class ApexLoopDevices:
    """Lease idle loops and allocate new ones without exposing occupied devices.

    The control interface is global to the guest. Existing idle devices are
    restored, newly allocated devices are removed, and occupied devices stay
    outside the namespace. Partial allocation also runs ownership-aware cleanup.
    """

    def __init__(self, output, packages):
        self.output, self.packages = Path(output), Path(packages).resolve()
        self.created = []
        self.control = None

    def __enter__(self):
        self.before = snapshot()
        numbers = {int(name[4:]) for name in self.before}
        if numbers != set(range(max(numbers, default=-1) + 1)):
            raise ValueError("original apexd requires contiguous pre-existing loop IDs")
        self.idle = [int(name[4:]) for name, value in self.before.items()
                     if not value.get("loop/backing_file")]
        self.output.mkdir(mode=0o700, exist_ok=False)
        (self.output / "block").mkdir()
        self.control = os.open(LOOP_CONTROL, os.O_RDWR | os.O_CLOEXEC)
        try:
            first = max((int(name[4:]) for name in self.before), default=-1) + 1
            count = max(32, 2 * len(list(self.packages.glob("*.apex"))))
            for number in range(first, first + count):
                fcntl.ioctl(self.control, LOOP_CTL_ADD, number)
                self.created.append(number)
            for number in self.idle + self.created:
                major, minor = map(int, (SYS_BLOCK / f"loop{number}/dev").read_text().split(":"))
                os.mknod(self.output / f"block/loop{number}", stat.S_IFBLK | 0o600,
                         os.makedev(major, minor))
            return self.output
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        errors = []
        ownership = []
        used = snapshot()
        for number in reversed(self.idle + self.created):
            name = f"loop{number}"
            backing = used.get(name, {}).get("loop/backing_file", "")
            try:
                if backing:
                    # Never detach a loop that another guest process has acquired.
                    # After pivot_root/umount oldroot, sysfs may show /<basename>.
                    # The kernel's backing device/inode identifies the actual file.
                    node = self.output / "block" / name
                    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
                    try:
                        status = fcntl.ioctl(fd, LOOP_GET_STATUS64, bytes(232))
                        identity = struct.unpack_from("=QQ", status)
                        matched = [p.name for p in self.packages.glob("*.apex")
                                   if (p.stat().st_dev, p.stat().st_ino) == identity]
                        ownership.append({"device": name, "matched_packages": matched})
                        if len(matched) != 1:
                            raise ValueError("unexpected loop owner: " + name)
                        fcntl.ioctl(fd, LOOP_CLR_FD)
                    finally:
                        os.close(fd)
                if number in self.created:
                    fcntl.ioctl(self.control, LOOP_CTL_REMOVE, number)
                elif "queue/read_ahead_kb" in self.before[name]:
                    (SYS_BLOCK / name / "queue/read_ahead_kb").write_text(
                        self.before[name]["queue/read_ahead_kb"] + "\n")
            except (OSError, ValueError) as error:
                errors.append({"device": name, "error": str(error)})
        os.close(self.control)
        after = snapshot()
        unchanged = all(after.get(name) == value for name, value in self.before.items())
        result = {"before": self.before, "used": used, "created": self.created, "leased_idle": self.idle,
                  "after": after, "cleanup_errors": errors,
                  "ownership_checks": ownership,
                  "preexisting_devices_unchanged": unchanged,
                  "created_devices_removed": all(f"loop{n}" not in after for n in self.created)}
        with (self.output / "result.json").open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        if errors or not unchanged or not result["created_devices_removed"]:
            raise RuntimeError("APEX loop cleanup requires inspection; see apex-loops/result.json")
