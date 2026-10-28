"""Consume one explicit native backup request after the preference owner exits.

This is an isolated Linux filesystem backend, not Android BackupManager parity.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


REQUEST = re.compile(rb"nashura\.native-backup\.request\.v1\n([0-9a-f]{32})\n")


def private_regular(descriptor, mode=0o600):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
            metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != mode):
        raise ValueError("private single-link regular file required")
    return metadata


def read_file(directory, name, limit, mode=0o600):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        metadata = private_regular(descriptor, mode)
        if not 0 < metadata.st_size <= limit:
            raise ValueError("file length outside native backend contract")
        data = b""
        while len(data) <= limit:
            block = os.read(descriptor, min(8192, limit + 1 - len(data)))
            if not block:
                break
            data += block
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(data) != metadata.st_size or any(getattr(after, field) != getattr(metadata, field) for field in fields):
            raise ValueError("file changed during bounded read")
        return data
    finally:
        os.close(descriptor)


def write_file(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(data):
            count = os.write(descriptor, data[offset:])
            if count <= 0:
                raise OSError("short archive write")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_archive(directory, expected, contents):
    if set(os.listdir(directory)) != {"manifest.json", *contents}:
        raise ValueError("archive members differ")
    if json.loads(read_file(directory, "manifest.json", 8192)) != expected:
        raise ValueError("existing receipt differs")
    for name, data in contents.items():
        if read_file(directory, name, 65536) != data:
            raise ValueError("archive bytes differ")


def consume(path):
    if not path.is_absolute():
        raise ValueError("absolute private spool directory required")
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    locks = []
    try:
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("private spool owner/mode differs")
        for name in (".preference.lock", ".backup.lock"):
            descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            locks.append(descriptor)
            private_regular(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if ".backup-request.tmp" in os.listdir(directory):
            raise ValueError("incomplete enqueue retained; explicit investigation required")
        request = read_file(directory, ".backup-pending", 128)
        match = REQUEST.fullmatch(request)
        if not match:
            raise ValueError("unknown native backup request")
        contents = {f"store-{slot}.xml": read_file(directory, f"store-{slot}.xml", 65536, 0o660)
                    for slot in range(2)}
        if any(f"store-{slot}.xml.bak" in os.listdir(directory) for slot in range(2)):
            raise ValueError("preference recovery incomplete; preserve pending request")
        report = {"schema": "nashura.native-backup-receipt.v1",
                  "request_sha256": hashlib.sha256(request).hexdigest(),
                  "request_id": match[1].decode(), "scope": "native_filesystem_archive_only",
                  "android_backup_parity": "NOT_VERIFIED",
                  "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                            for name, data in contents.items()}}
        name = "backup-" + report["request_id"]
        if name not in os.listdir(directory):
            temporary = Path(tempfile.mkdtemp(prefix=".backup-consume-", dir=path))
            for member, data in contents.items():
                write_file(temporary / member, data)
            write_file(temporary / "manifest.json", (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
            staged = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(staged)
            finally:
                os.close(staged)
            os.rename(temporary.name, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        archived = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            verify_archive(archived, report, contents)
        finally:
            os.close(archived)
        os.unlink(".backup-pending", dir_fd=directory)
        os.fsync(directory)
        return report
    finally:
        for descriptor in reversed(locks):
            os.close(descriptor)
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    with arguments.output.open("x") as stream:
        try:
            report = {"status": "NATIVE_ARCHIVE_CONSUMED", **consume(arguments.directory)}
        except (ValueError, OSError) as error:
            json.dump({"status": "BLOCKED", "error": type(error).__name__ + ": " + str(error)}, stream)
            stream.write("\n")
            return 1
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
