"""Exercise a real Linux Binder device without contacting any Android service."""
import argparse
import fcntl
import json
import mmap
import os
import platform
import stat
import struct
import sys


def probe(device):
    result = {"schema": "nashura.binder-device-probe.v1", "device": str(device),
              "architecture": platform.machine(), "kernel": platform.release(),
              "status": "FAILED", "phase": "stat"}
    descriptor = None
    try:
        node = os.stat(device)
        result["character_device"] = stat.S_ISCHR(node.st_mode)
        if not result["character_device"]:
            raise ValueError("Binder requires a character device")
        result["device_major"] = os.major(node.st_rdev)
        result["device_minor"] = os.minor(node.st_rdev)
        result["phase"] = "open"
        descriptor = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        result["phase"] = "BINDER_VERSION"
        version = bytearray(struct.pack("=i", 0))
        fcntl.ioctl(descriptor, 0xC0046209, version, True)
        result["protocol_version"] = struct.unpack("=i", version)[0]
        if result["protocol_version"] != 8 or struct.calcsize("P") != 8:
            raise ValueError("probe requires the 64-bit Binder protocol")
        result["phase"] = "BINDER_SET_MAX_THREADS"
        fcntl.ioctl(descriptor, 0x40046205, struct.pack("=I", 1))
        result["phase"] = "mmap"
        with mmap.mmap(descriptor, 1024 * 1024, flags=mmap.MAP_PRIVATE | 0x4000,
                       prot=mmap.PROT_READ) as mapping:
            result["mapped_bytes"] = len(mapping)
            result["phase"] = "BINDER_WRITE_READ"
            empty = bytearray(48)
            fcntl.ioctl(descriptor, 0xC0306201, empty, True)
            result["empty_write_read_returned"] = empty == bytearray(48)
            if not result["empty_write_read_returned"]:
                raise ValueError("unexpected empty transaction result")
        result.update(status="PASS_LIMITED", phase="complete",
                      android_services_contacted=False)
    except (OSError, ValueError) as error:
        result["error_type"] = type(error).__name__
        result["error"] = str(error)
        if isinstance(error, OSError):
            result["errno"] = error.errno
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/binderfs/binder")
    args = parser.parse_args()
    result = probe(args.device)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS_LIMITED" else 1


if __name__ == "__main__":
    sys.exit(main())
