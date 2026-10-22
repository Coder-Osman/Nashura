"""Real-engine P2 smoke: ACT plan execution confirmation and cancellation.

Drives one fresh native match through the correctness-first event loop.  At a
public event it installs a legal immediate card plan and verifies the engine
publishes the actor's card play (execution confirmation), then installs a long
delay plan and injects an opponent play to force an intervening event before the
scheduled tick, verifying the plan is cancelled and never executed.

Usage:
    .venv/bin/python tools/event_p2_act_smoke.py --port 26789
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.battle_env import BattleEnvV1  # noqa: E402
from native_runner.cr_native_env import (  # noqa: E402
    HandAction,
    ResidentNativeClashEnv,
)
from native_runner.match_factory import MatchConfig  # noqa: E402
from native_runner.resident_batch_vector import (  # noqa: E402
    ResidentBatchCoordinatorV1,
    ResidentBatchNativeProxyV1,
)
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog  # noqa: E402
from native_runner.training.event_config import EventPolicyConfigV1  # noqa: E402
from native_runner.training.event_driven import EventActionTargetV1  # noqa: E402
from native_runner.training.event_driven_runtime import EventDrivenRuntimeV1  # noqa: E402
from native_runner.training.v4.factory import production_semantic_bundle  # noqa: E402

DECK = [26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030]


def _legal_plan(environment: BattleEnvV1, observations, owner: int):
    """Return (card_id, cell) for a hand card with a legal placement, or None."""

    player = next((item for item in observations[owner].players if item.owner == owner), None)
    if player is None or not player.hand:
        return None
    mask = environment.action_mask(owner)
    if not mask.kinds.get("play_card", False):
        return None
    for slot, card_id in enumerate(player.hand[:4]):
        if slot >= len(mask.hand_slots) or not mask.hand_slots[slot]:
            continue
        entry = mask.placement_masks.get(str(slot))
        if not isinstance(entry, Mapping):
            continue
        rows = entry.get("row_major")
        if not rows:
            continue
        for y, row in enumerate(rows):
            for x, value in enumerate(row):
                if value:
                    return int(card_id), (int(x), int(y))
    return None


def _actor_plays(environment: BattleEnvV1, owner: int) -> list[dict]:
    return [
        item
        for item in environment.deployment_timing_events(owner)
        if str(item.get("kind")) == "card_play"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--env-id", type=int, default=15)
    parser.add_argument("--max-waits", type=int, default=12)
    args = parser.parse_args()

    bundle = production_semantic_bundle()
    native = ResidentNativeClashEnv(args.env_id, args.host, args.port, timeout=60.0)
    proxy = ResidentBatchNativeProxyV1(
        native, ResidentBatchCoordinatorV1((native,), timeout=60.0, training_capture=True)
    )
    environment = BattleEnvV1(
        native=proxy,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
    )
    report: dict[str, object] = {"port": args.port}
    try:
        environment.reset(
            match_config=MatchConfig(deck0=DECK, deck1=DECK, seed=8_100_000 + args.env_id),
            options={
                "render_mode": "headless",
                "decision_ticks": 5,
                "event_driven_decisions": False,
                "allow_initial_remaining_runtime_rejections": True,
            },
        )
        runtime = EventDrivenRuntimeV1(
            environment, owner=0, config=EventPolicyConfigV1(max_wait_ticks=60)
        )

        # Phase 1: install a legal immediate plan and confirm native execution.
        execution = None
        for _wait in range(args.max_waits):
            observations, events, terminal = runtime.advance_until_event()
            if terminal:
                break
            plan = _legal_plan(environment, observations, 0)
            if plan is None:
                player = next((item for item in observations[0].players if item.owner == 0), None)
                mask = environment.action_mask(0)
                report["debug"] = {
                    "tick": int(observations[0].tick),
                    "hand": list(player.hand) if player else None,
                    "elixir": player.elixir_exact if player else None,
                    "kinds": dict(mask.kinds),
                    "hand_slots": list(mask.hand_slots),
                    "placement_keys": sorted(mask.placement_masks.keys()),
                }
                runtime.install_plan(EventActionTargetV1(owner=0, wait=True, source_tick=int(observations[0].tick)))
                continue
            card_id, cell = plan
            install_tick = int(observations[0].tick)
            before = len(_actor_plays(environment, 0))
            runtime.install_plan(
                EventActionTargetV1(
                    owner=0,
                    wait=False,
                    card_id=card_id,
                    target_cell=cell,
                    delay_ms=0,
                    source_tick=install_tick,
                )
            )
            _obs, _events, terminal = runtime.advance_until_event()
            after = _actor_plays(environment, 0)
            executed = runtime.executed_plans
            confirmed = any(int(item.get("cardId") or -1) == card_id for item in after[before:])
            execution = {
                "install_tick": install_tick,
                "card_id": card_id,
                "cell": list(cell),
                "executed_plans": executed,
                "actor_plays_added": len(after) - before,
                "confirmed_card_play": confirmed,
                "terminal": terminal,
            }
            break
        report["execution"] = execution

        # Phase 2: long delay plan must be cancelled by an intervening event.
        cancellation = None
        if execution is not None:
            for _wait in range(args.max_waits):
                observations, events, terminal = runtime.advance_until_event()
                if terminal:
                    break
                plan = _legal_plan(environment, observations, 0)
                if plan is None:
                    runtime.install_plan(
                        EventActionTargetV1(owner=0, wait=True, source_tick=int(observations[0].tick))
                    )
                    continue
                card_id, cell = plan
                install_tick = int(observations[0].tick)
                before = len(_actor_plays(environment, 0))
                runtime.install_plan(
                    EventActionTargetV1(
                        owner=0,
                        wait=False,
                        card_id=card_id,
                        target_cell=cell,
                        delay_ms=3000,
                        source_tick=install_tick,
                    )
                )
                # Inject an opponent play two ticks ahead: it becomes a public
                # event well before the planned tick.
                opponent = _legal_plan(environment, observations, 1)
                injected = None
                if opponent is not None:
                    opp_card, opp_cell = opponent
                    raw = environment.raw_observation
                    player1 = next(item for item in raw["players"] if int(item["owner"]) == 1)
                    slot = next(
                        (
                            int(item["handIndex"])
                            for item in player1["hand"]
                            if int(item["cardId"]) == opp_card
                        ),
                        None,
                    )
                    if slot is not None:
                        queued = environment.native.queue_hand_action_at(
                            HandAction(1, slot, opp_cell[0] * 1000 + 500, opp_cell[1] * 1000 + 500),
                            execute_in_ticks=2,
                        )
                        injected = {"card_id": opp_card, "cell": list(opp_cell), "slot": slot, "queued": bool(queued)}
                cancelled_before = runtime.cancelled_plans
                _obs, _events, terminal = runtime.advance_until_event()
                after = _actor_plays(environment, 0)
                cancellation = {
                    "install_tick": install_tick,
                    "planned_card": card_id,
                    "cancelled_plans_delta": runtime.cancelled_plans - cancelled_before,
                    "executed_plans": runtime.executed_plans,
                    "planned_card_played": any(
                        int(item.get("cardId") or -1) == card_id for item in after[before:]
                    ),
                    "opponent_injected": injected,
                    "terminal": terminal,
                }
                break
        report["cancellation"] = cancellation
        report["cancelled_plans_total"] = runtime.cancelled_plans
        report["executed_plans_total"] = runtime.executed_plans
    finally:
        try:
            proxy.close()
        except Exception:
            pass
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    ok = bool(report.get("execution") and report["execution"].get("confirmed_card_play"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
