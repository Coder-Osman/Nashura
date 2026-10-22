"""Run the correctness-first P2 event loop against one resident native slot."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.battle_env import BattleEnvV1  # noqa: E402
from native_runner.contracts import ActionV1  # noqa: E402
from native_runner.cr_native_env import ResidentNativeClashEnv  # noqa: E402
from native_runner.match_factory import MatchConfig  # noqa: E402
from native_runner.resident_batch_vector import (  # noqa: E402
    ResidentBatchCoordinatorV1,
    ResidentBatchNativeProxyV1,
)
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog  # noqa: E402
from native_runner.training.event_config import EventPolicyConfigV1  # noqa: E402
from native_runner.training.event_driven import EventActionTargetV1  # noqa: E402
from native_runner.training.event_driven_runtime import EventDrivenRuntimeV1  # noqa: E402
from native_runner.training.v4.expert import POLICY_DECISION_TICKS  # noqa: E402
from native_runner.training.v4.factory import production_semantic_bundle  # noqa: E402

DECK = [26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--env-id", type=int, default=15)
    parser.add_argument("--max-events", type=int, default=12)
    parser.add_argument("--max-wait-ticks", type=int, default=300)
    args = parser.parse_args()

    bundle = production_semantic_bundle()
    native = ResidentNativeClashEnv(args.env_id, args.host, args.port, timeout=60.0)
    proxy = ResidentBatchNativeProxyV1(
        native,
        ResidentBatchCoordinatorV1((native,), timeout=60.0, training_capture=True),
    )
    environment = BattleEnvV1(
        native=proxy,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
    )
    try:
        environment.reset(
            match_config=MatchConfig(deck0=DECK, deck1=DECK, seed=8_000_000 + args.env_id),
            options={
                "render_mode": "headless",
                "decision_ticks": POLICY_DECISION_TICKS,
                "event_driven_decisions": False,
                "allow_initial_remaining_runtime_rejections": True,
            },
        )
        runtime = EventDrivenRuntimeV1(
            environment,
            owner=0,
            config=EventPolicyConfigV1(max_wait_ticks=args.max_wait_ticks),
        )
        event_rows = []
        started = time.perf_counter()
        for index in range(args.max_events):
            observations, events, terminal = runtime.advance_until_event()
            event_rows.extend(
                {
                    "tick": event.tick,
                    "type": event.event_type,
                    "owner": event.owner,
                    "card_id": event.card_id,
                }
                for event in events
            )
            print(
                json.dumps(
                    {
                        "wake": index,
                        "tick": int(observations[0].tick),
                        "events": event_rows[-len(events) :] if events else [],
                        "terminal": terminal,
                        "cancelled_plans": runtime.cancelled_plans,
                        "executed_plans": runtime.executed_plans,
                        "wait_wakes": runtime.wait_wakes,
                    }
                ),
                flush=True,
            )
            if terminal:
                break
            # Keep the first P2 run observational: wait for a new event rather
            # than injecting a guessed card plan into the real engine.
            runtime.install_plan(
                EventActionTargetV1(owner=0, wait=True, source_tick=int(observations[0].tick))
            )
        elapsed = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "summary": {
                        "events": len(event_rows),
                        "deployment_events": sum(row["type"] == "deployment" for row in event_rows),
                        "elapsed_seconds": round(elapsed, 3),
                        "event_rows": event_rows,
                    }
                }
            ),
            flush=True,
        )
    finally:
        try:
            proxy.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
