"""Recover only a proven interrupted native backup hard-link publication."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat

from native_backup_consume import REQUEST, private_regular


def recover(path):
    if not path.is_absolute():
        raise ValueError("absolute private spool directory required")
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    descriptors = []
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private spool owner/mode differs")
        for name in (".preference.lock", ".backup.lock"):
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            descriptors.append(fd)
            private_regular(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = []
        for name in (".backup-pending", ".backup-request.tmp"):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 2 or info.st_size > 128):
                raise ValueError("publication file identity or mode differs")
            data = os.read(fd, 129)
            if len(data) != info.st_size or not REQUEST.fullmatch(data):
                raise ValueError("invalid publication record")
            records.append((info, data))
        first, second = records
        if (first[0].st_dev, first[0].st_ino, first[1]) != (second[0].st_dev, second[0].st_ino, second[1]):
            raise ValueError("pending and staging are not the same record")
        # Locks protect cooperating producers; recheck names before the unlink.
        for name in (".backup-pending", ".backup-request.tmp"):
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_nlink) != (first[0].st_dev, first[0].st_ino, 2):
                raise ValueError("publication changed during recovery")
        os.unlink(".backup-request.tmp", dir_fd=directory)
        os.fsync(directory)
        if os.fstat(descriptors[-2]).st_nlink != 1:
            raise ValueError("pending link count after recovery")
        return {"status": "PENDING_RECOVERED", "request_id": REQUEST.fullmatch(first[1])[1].decode(),
                "android_backup_parity": "NOT_VERIFIED", "catalog_ready_pass": False}
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as stream:
        try:
            result = recover(args.directory)
        except (OSError, ValueError) as error:
            json.dump({"status": "BLOCKED", "error": type(error).__name__ + ": " + str(error)}, stream)
            stream.write("\n")
            return 1
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
