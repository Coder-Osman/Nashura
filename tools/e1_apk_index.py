"""Build a strict read-only APK asset index without publishing asset content."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import struct
import zipfile


def build_index(apk):
    rows = []
    seen = set()
    with zipfile.ZipFile(apk) as archive, apk.open("rb") as source:
        for entry in archive.infolist():
            if not entry.filename.startswith("assets/") or entry.is_dir():
                continue
            name = entry.filename[len("assets/"):]
            if (not name or name.startswith("/") or any(part in ("", ".", "..")
                    for part in name.split("/")) or any(character in name for character in "\t\r\n\\")
                    or PurePosixPath(name).is_absolute() or name in seen):
                raise ValueError("invalid or duplicate asset path")
            if entry.flag_bits & 1 or entry.compress_type not in (0, 8):
                raise ValueError("unsupported asset encryption or compression")
            if max(entry.file_size, entry.compress_size) > 2**31 - 1:
                raise ValueError("asset exceeds provider size contract")
            source.seek(entry.header_offset)
            header = source.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise ValueError("invalid local ZIP header")
            fields = struct.unpack("<4s5H3I2H", header)
            filename = source.read(fields[-2])
            encoding = "utf-8" if entry.flag_bits & 0x800 else "cp437"
            if filename.decode(encoding) != entry.filename or fields[3] != entry.compress_type:
                raise ValueError("ZIP directory and local entry disagree")
            offset = entry.header_offset + 30 + fields[-2] + fields[-1]
            if offset < 0 or offset + entry.compress_size > apk.stat().st_size:
                raise ValueError("asset outside APK")
            seen.add(name)
            rows.append((name, offset, entry.file_size, entry.compress_size, entry.compress_type, entry.CRC))
    if not rows:
        raise ValueError("APK contains no assets")
    return sorted(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = build_index(args.apk)
    with args.output.open("x") as target:
        target.writelines("\t".join(map(str, row)) + "\n" for row in rows)
    print(json.dumps(dict(files=len(rows), bytes=sum(row[2] for row in rows),
                          apk_sha256=hashlib.sha256(args.apk.read_bytes()).hexdigest(),
                          index_sha256=hashlib.sha256(args.output.read_bytes()).hexdigest())))


if __name__ == "__main__":
    main()
