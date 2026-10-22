"""Merge several event observation cache partitions into one training root.

Shard files are symlinked (not copied) so merging stays cheap, and the merged
``index.json`` reuses the standard writer so the pretraining entry can consume
the result directly.

Usage:
    python tools/merge_event_cache.py --output MERGED --input PART0 PART1 ...
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.training.event_cache import write_event_cache_index  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    shards: list[dict[str, object]] = []
    stats: list[dict[str, object]] = []
    contract: dict[str, object] | None = None
    for root in args.input:
        root = root.resolve()
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        if contract is None:
            contract = dict(index.get("contract", {}))
        for entry in index.get("shards", []):
            summary_name = str(entry["summary_file"])
            data_name = str(entry["data_file"])
            for name in (summary_name, data_name):
                link = output / name
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(root / name)
            merged = dict(entry)
            merged["source_root"] = str(root)
            shards.append(merged)
        stats_path = root / "stats.json"
        if stats_path.is_file():
            stats.extend(json.loads(stats_path.read_text(encoding="utf-8")))
    if not shards:
        raise ValueError("no shards were merged")
    shards.sort(key=lambda item: (str(item.get("source_root")), str(item["shard_id"])))
    index = write_event_cache_index(output / "index.json", shards, contract=contract or {})
    (output / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "shards": index["shard_count"],
                "frames": index["frames"],
                "act_frames": index["act_frames"],
                "legal_act_frames": index["legal_act_frames"],
                "replays": index["replays"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
