"""Diagnose native event-ring timing for event-boundary capture.

Two facts this probe establishes for the event producer:

1. A play recorded at tick P appears in the public deployment ring with
   ``tick == P`` but is only observable after stepping to tick ``P+1``.
2. The deployed entity itself is likewise absent from the observation at tick
   ``P`` and present at tick ``P+1``.

Therefore the event observation tick is ``play_tick + 1`` for both the
schedule-driven capture and the ring-driven P2 runtime.

Usage:
    .venv/bin/python tools/event_ring_probe.py --port 26789 --dataset-root <root>
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.cr_native_env import NativeClashEnv  # noqa: E402
from native_runner.royaleapi_replay import (  # noqa: E402
    calibrate_collected_replay_deal,
    prepare_collected_replay,
)
from native_runner.training.v4.cache_builder import _headless_calibration_probe  # noqa: E402
from native_runner.training.v4.dataset import ParquetReplayStreamV4  # noqa: E402
from native_runner.training.v4.factory import (  # noqa: E402
    build_collected_replay_environment_v4,
    build_episode_tensorizers_v4,
)
from native_runner.training.v4.producer import (  # noqa: E402
    _build_il_lane,
    _initialize_il_lane,
    _wait_group,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--dataset-root", type=Path, default=Path("/home/gujiga/firstlight_data/IL_Replay"))
    parser.add_argument("--ticks", type=int, default=400)
    args = parser.parse_args()

    native = NativeClashEnv(port=args.port)
    native.wait_ready(60)
    probe = _headless_calibration_probe(native)
    payload, _report = next(iter(ParquetReplayStreamV4(args.dataset_root)))
    prepared = prepare_collected_replay(payload)
    calibrated = calibrate_collected_replay_deal(prepared, probe)
    tensorizers = build_episode_tensorizers_v4(calibrated.replay.episode_config)
    environment = build_collected_replay_environment_v4(calibrated, native=native)
    lane = _build_il_lane(environment, calibrated, tensorizers)
    _initialize_il_lane(lane)
    plays = sorted({int(item.source_tick) for item in lane.expert_actions})
    first = plays[0] if plays else 0
    print("replay", calibrated.replay_tag, "end", lane.source_end_tick, "first_plays", plays[:6])
    seen: set[str] = set()
    try:
        for _ in range(args.ticks):
            observations, _reward, terminated, truncated, _info = environment.step(
                _wait_group(1), record_trace=False
            )
            tick = int(observations[0].tick)
            ring = []
            for owner in (0, 1):
                for item in environment.deployment_timing_events(owner):
                    key = str(item.get("nativeEventId"))
                    if key in seen:
                        continue
                    seen.add(key)
                    ring.append((item.get("kind"), int(item["tick"]), item.get("owner")))
            if first - 2 <= tick <= first + 2:
                cards = sorted({(entity.owner, entity.card_id) for entity in observations[0].entities if entity.card_id})
                print("obs_tick", tick, "entities", len(observations[0].entities), "cards", cards[-4:], "ring", ring)
            if bool(terminated[0]) or bool(truncated[0]):
                print("terminal at", tick)
                break
    finally:
        try:
            native.stop_resident_mode()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
