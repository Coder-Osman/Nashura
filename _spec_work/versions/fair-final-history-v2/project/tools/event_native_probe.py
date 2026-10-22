"""Validate the native step-until-event batch primitive against a known replay.

Builds one resident IL lane from a collected replay (so the renderer schedules
every expert card play), initializes it, then opens a batch session in
step-until-event mode and repeatedly asks the engine to advance to the next
public deployment.  The reported stop ticks must equal the recorded play ticks
plus one (the tick where the deployment first becomes observable), and the
number of engine round trips must equal the number of distinct events.

Usage:
    .venv/bin/python tools/event_native_probe.py --port 26789 --dataset-root <root>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

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
    build_resident_training_proxies_v4,
)
from native_runner.training.v4.producer import (  # noqa: E402
    _build_il_lane,
    _initialize_il_lane,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dataset-root", type=Path, default=Path("/home/gujiga/firstlight_data/IL_Replay"))
    parser.add_argument("--max-ticks", type=int, default=300)
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--capture-mode", choices=("json", "compact"), default="json")
    args = parser.parse_args()

    control = NativeClashEnv(host=args.host, port=args.port)
    control.wait_ready(60)
    probe = _headless_calibration_probe(control)
    payload, _report = next(iter(ParquetReplayStreamV4(args.dataset_root)))
    prepared = prepare_collected_replay(payload)
    calibrated = calibrate_collected_replay_deal(prepared, probe)
    tensorizers = build_episode_tensorizers_v4(calibrated.replay.episode_config)

    proxies, coordinator = build_resident_training_proxies_v4(
        (0,), host=args.host, port=args.port, timeout=60.0, capture_mode=args.capture_mode, step_until_event=True
    )
    proxy = proxies[0]
    environment = build_collected_replay_environment_v4(calibrated, native=proxy)
    lane = _build_il_lane(environment, calibrated, tensorizers)
    _initialize_il_lane(lane)
    plays = sorted({int(item.source_tick) for item in lane.expert_actions if item.action.kind.value == "play_card"})
    expected = sorted({tick + 1 for tick in plays if tick + 1 < lane.source_end_tick})
    print(
        json.dumps(
            {
                "replay": calibrated.replay_tag,
                "source_end": lane.source_end_tick,
                "plays": len(plays),
                "expected_stop_ticks": len(expected),
                "start_tick": lane.current_tick,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    coordinator.start_session()
    stops: list[tuple[int, int]] = []
    started = time.perf_counter()
    try:
        for _call in range(args.max_calls):
            coordinator.begin_round((0,))
            ordinary = proxy.step(args.max_ticks)
            coordinator.finish_round()
            tick = int(ordinary["tick"])
            flags = proxy.last_stop_flags
            stops.append((tick, flags))
            if proxy.stopped_on_terminal:
                break
            if len(stops) > 1 and tick <= stops[-2][0]:
                # No forward progress (for example a repeated max-tick wake).
                break
    finally:
        coordinator.close_session()
        try:
            control.stop_resident_mode()
        except Exception:
            pass
    elapsed = time.perf_counter() - started

    detected = [tick for tick, flags in stops if flags & (1 << 1)]
    terminal = [tick for tick, flags in stops if flags & (1 << 2)]
    maxed = [tick for tick, flags in stops if flags & (1 << 3)]
    matched = sorted(set(detected) & set(expected))
    missing = sorted(set(expected) - set(detected))
    extra = sorted(set(detected) - set(expected))
    print(
        json.dumps(
            {
                "calls": len(stops),
                "detected": len(detected),
                "matched": len(matched),
                "missing": len(missing),
                "extra": len(extra),
                "terminal_stops": terminal,
                "max_tick_stops": len(maxed),
                "elapsed_s": round(elapsed, 3),
                "first_detected": detected[:8],
                "first_expected": expected[:8],
                "missing_sample": missing[:8],
                "extra_sample": extra[:8],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
