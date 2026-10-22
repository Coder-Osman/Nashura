"""Convert HF IL_Replay shards into sparse event-policy samples.

Semantics follow EVENT_DRIVEN_SPEC: at every public deployment event (either
side) and for each owner, the target is that owner's next card play with a
delay label. WAIT is emitted when the owner has no later card play before the
match ends. No future-observation data is used as input; coordinates and card
keys are labels only.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

REPLAY_OWNER = {"team": 0, "opponent": 1}
MAX_DELAY_MS = 6000
DELAY_STEP_MS = 100


def _read_rows(path: Path):
    import pyarrow.parquet as parquet

    return parquet.read_table(path).to_pylist()


def _quantize_delay_ms(delay_ticks: int) -> int:
    ms = max(0, int(delay_ticks)) * 50
    quantized = int(round(ms / DELAY_STEP_MS) * DELAY_STEP_MS)
    return min(MAX_DELAY_MS, quantized)


def convert(
    replays_path: Path,
    actions_path: Path,
    output_path: Path,
    *,
    max_replays: int,
    write_events: bool = True,
) -> dict[str, object]:
    replay_tags = {str(row["replay_tag"]) for row in _read_rows(replays_path)}
    actions = [
        row
        for row in _read_rows(actions_path)
        if str(row.get("replay_tag", "")) in replay_tags
    ]
    grouped: dict[str, list[dict]] = {}
    for row in actions:
        grouped.setdefault(str(row["replay_tag"]), []).append(row)

    selected = sorted(replay_tags)[: max_replays] if max_replays > 0 else sorted(replay_tags)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    delay_hist: Counter[int] = Counter()
    per_replay_events: list[int] = []
    stream = output_path.open("w", encoding="utf-8") if write_events else None
    try:
        for tag in selected:
            rows = grouped.get(tag, ())
            counts["replays_selected"] += 1
            plays = []
            for row in rows:
                kind = str(row.get("kind", ""))
                if kind == "activate_ability":
                    counts["ability_rows"] += 1
                    continue
                if kind != "play_card":
                    counts[f"unknown_kind_{kind or 'empty'}"] += 1
                    continue
                side = str(row.get("side", ""))
                owner = REPLAY_OWNER.get(side)
                tick = row.get("replay_tick_20hz")
                grid_x, grid_y = row.get("grid_x"), row.get("grid_y")
                card_key = row.get("card_key")
                if owner is None or tick is None:
                    counts["play_rows_missing_owner_or_tick"] += 1
                    continue
                if card_key in (None, ""):
                    counts["play_rows_missing_card"] += 1
                    continue
                if grid_x is None or grid_y is None or not (0 <= int(grid_x) < 18 and 0 <= int(grid_y) < 32):
                    counts["play_rows_invalid_grid"] += 1
                    continue
                plays.append(
                    {
                        "tick": int(tick),
                        "owner": owner,
                        "card_key": str(card_key),
                        "grid": [int(grid_x), int(grid_y)],
                        "source_index": int(row.get("source_index") or 0),
                    }
                )
            plays.sort(key=lambda item: (item["tick"], item["source_index"]))
            if not plays:
                counts["replays_without_valid_plays"] += 1
                continue
            counts["replays_with_valid_plays"] += 1
            counts["valid_play_rows"] += len(plays)
            per_replay_events.append(len(plays))
            for owner in (0, 1):
                own = [item for item in plays if item["owner"] == owner]
                for index, event in enumerate(plays):
                    trigger_tick = event["tick"]
                    # A play recorded at tick P is first visible at observation
                    # tick P+1 (engine probe: play 216 -> entity and ring at
                    # 217), so the decision tick is P+1 and the play that caused
                    # the trigger is never its own answer.
                    event_tick = trigger_tick + 1
                    following = next((item for item in own if item["tick"] > trigger_tick), None)
                    horizon_ticks = MAX_DELAY_MS // 50
                    if following is None or (following["tick"] - event_tick) > horizon_ticks:
                        # No plan inside the horizon is a WAIT: the next event
                        # (either side) would cancel a far-future plan anyway.
                        counts[f"owner_{owner}_wait"] += 1
                        counts[f"owner_{owner}_wait_beyond_horizon"] += int(following is not None)
                        target = {"wait": True, "card_key": None, "grid": None, "delay_ms": 0}
                    else:
                        delay_ms = _quantize_delay_ms(max(0, following["tick"] - event_tick))
                        delay_hist[delay_ms] += 1
                        counts[f"owner_{owner}_act"] += 1
                        target = {
                            "wait": False,
                            "card_key": following["card_key"],
                            "grid": following["grid"],
                            "delay_ms": delay_ms,
                        }
                    counts["samples"] += 1
                    if stream is not None:
                        stream.write(
                            json.dumps(
                                {
                                    "schema": "event-training-row.v1",
                                    "replay_tag": tag,
                                    "owner": owner,
                                    "trigger": {"tick": trigger_tick, "observation_tick": event_tick},
                                    "target": target,
                                    "source": {
                                        "trigger_source_index": event["source_index"],
                                        "target_source_index": (
                                            following["source_index"] if following is not None else None
                                        ),
                                        "target_tick": following["tick"] if following is not None else None,
                                    },
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
    finally:
        if stream is not None:
            stream.close()

    samples = max(1, counts["samples"])
    acts = counts["owner_0_act"] + counts["owner_1_act"]
    waits = counts["owner_0_wait"] + counts["owner_1_wait"]
    report: dict[str, object] = {
        "schema": "event-training-quality-report.v1",
        "replays_path": str(replays_path),
        "actions_path": str(actions_path),
        "output_path": str(output_path),
        "max_replays": max_replays,
        "counts": dict(counts),
        "event_count": {
            "min": min(per_replay_events) if per_replay_events else 0,
            "mean": (sum(per_replay_events) / len(per_replay_events)) if per_replay_events else 0.0,
            "max": max(per_replay_events) if per_replay_events else 0,
        },
        "delay_ms_histogram": dict(sorted(delay_hist.items())),
        "acceptance": {
            "join_coverage": counts["replays_with_valid_plays"] / max(1, counts["replays_selected"]),
            "act_fraction": acts / samples,
            "wait_fraction": waits / samples,
            "act_wait_ratio": (acts / waits) if waits else float("inf"),
            "delay_label_coverage": acts / samples,
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/firstlight/datasets/IL_Replay"))
    parser.add_argument("--output", type=Path, default=Path("/data/firstlight/datasets/IL_Replay/event-training.jsonl"))
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--max-replays", type=int, default=2000)
    args = parser.parse_args()
    report = convert(
        args.root / "replays/part-000000.parquet",
        args.root / "actions/part-000000.parquet",
        args.output,
        max_replays=args.max_replays,
    )
    report_path = args.report or args.output.with_suffix(".quality.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
