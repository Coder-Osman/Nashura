"""Compare original serializer payloads from paired headless runs.

Each private archive contains repeated little-endian (tick:int32, size:int32)
headers followed by exactly size payload bytes. Missing ticks, malformed frames,
trailing bytes and a wrong final tick are errors, not partial comparisons.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

HEADER = struct.Struct("<ii")
MAX_PAYLOAD = 32 * 1024 * 1024


def inspect_archive(path, expected_final_tick):
    digest = hashlib.sha256()
    frames = []
    total = 0
    with Path(path).open("rb") as source:
        for expected_tick in range(expected_final_tick + 1):
            header = source.read(HEADER.size)
            if len(header) != HEADER.size:
                raise ValueError(f"missing or truncated header at tick {expected_tick}")
            tick, size = HEADER.unpack(header)
            if tick != expected_tick or not 0 < size <= MAX_PAYLOAD:
                raise ValueError(f"invalid tick or payload size at tick {expected_tick}")
            payload = source.read(size)
            if len(payload) != size:
                raise ValueError(f"truncated payload at tick {tick}")
            digest.update(header)
            digest.update(payload)
            total += HEADER.size + size
            frames.append({"tick": tick, "bytes": size,
                           "sha256": hashlib.sha256(payload).hexdigest()})
        if source.read(1):
            raise ValueError("extra frames or trailing bytes after final tick")
    return {"bytes": total, "sha256": digest.hexdigest(), "frames": frames}


def compare(left, right, final_tick):
    if not 0 <= final_tick <= 1000000:
        raise ValueError("expected final tick outside supported range")
    if Path(left).resolve() == Path(right).resolve():
        raise ValueError("paired archives must be distinct files")
    a = inspect_archive(left, final_tick)
    b = inspect_archive(right, final_tick)
    differences = [x["tick"] for x, y in zip(a["frames"], b["frames"]) if x != y]
    return {"schema": "nashura.headless-snapshot-comparison.v1",
            "first_tick": 0, "final_tick": final_tick, "records": final_tick + 1,
            "equal": not differences and a["sha256"] == b["sha256"],
            "different_ticks": differences,
            "left": a, "right": b,
            "scope": "original serializer payloads; excludes process/global state outside serialization"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--expected-final-tick", required=True, type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare(args.left, args.right, args.expected_final_tick)
    if args.output:
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(result, target, indent=2)
            target.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key not in ("left", "right")}))
    raise SystemExit(0 if result["equal"] else 1)


if __name__ == "__main__":
    main()
