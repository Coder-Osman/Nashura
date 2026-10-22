"""Resident multi-match smoke test: N lanes on one engine over the batch data plane.

Validates, in order: resident mode activation via ``env N configure``, the
coordinator's persistent batch session (batch-v1/batch-fast handshake), compact
CRTF capture decoding, BattleEnvV1 stepping through the proxies, staggered lane
termination, and a full reset cycle. Reports per-lane ticks, aggregate tps, and
the coordinator channel profile.

Usage:
    python tools/resident_smoke.py --port 26789 --lanes 2 --max-ticks 1200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DECK = [26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--lanes", type=int, default=2)
    parser.add_argument("--max-ticks", type=int, default=1200)
    parser.add_argument("--capture-mode", default="compact", choices=("compact", "compact-raw", "zlib-json", "json"))
    parser.add_argument("--episodes", type=int, default=1, help="full reset cycles to run")
    parser.add_argument("--deck", default=None, help="comma-separated card ids overriding the default deck")
    args = parser.parse_args()

    import torch

    torch.set_num_threads(1)
    from native_runner.battle_env import BattleEnvV1
    from native_runner.contracts import ActionV1
    from native_runner.cr_native_env import ResidentNativeClashEnv
    from native_runner.match_factory import MatchConfig
    from native_runner.resident_batch_vector import ResidentBatchCoordinatorV1, ResidentBatchNativeProxyV1
    from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog
    from native_runner.training.v4.expert import POLICY_DECISION_TICKS
    from native_runner.training.v4.factory import production_semantic_bundle

    deck = [int(value) for value in args.deck.split(",")] if args.deck else DECK
    bundle = production_semantic_bundle()

    timing = {"step_s": 0.0, "observe_s": 0.0, "observe_calls": 0, "step_calls": 0}
    original_observe = BattleEnvV1.observe

    def timed_observe(self, *method_args, **method_kwargs):
        started = time.perf_counter()
        try:
            return original_observe(self, *method_args, **method_kwargs)
        finally:
            timing["observe_s"] += time.perf_counter() - started
            timing["observe_calls"] += 1

    BattleEnvV1.observe = timed_observe  # type: ignore[method-assign]

    natives = tuple(
        ResidentNativeClashEnv(env_id, args.host, args.port, timeout=60.0) for env_id in range(args.lanes)
    )
    coordinator = ResidentBatchCoordinatorV1(
        natives,
        timeout=60.0,
        training_capture=args.capture_mode in ("compact", "compact-raw"),
        compressed_json=args.capture_mode in ("compact", "zlib-json"),
        validate_json_capture=False,
    )
    proxies = tuple(ResidentBatchNativeProxyV1(native, coordinator) for native in natives)
    environments = tuple(
        BattleEnvV1(
            native=proxy,
            card_catalog=bundle.native_card_catalog,
            semantic_subset_contract=bundle.semantic_subset_contract,
            effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
        )
        for proxy in proxies
    )

    def run_episode(episode: int) -> dict[str, float]:
        coordinator.close_session()
        initial_ticks: dict[int, int] = {}
        for env_id, environment in enumerate(environments):
            started = time.perf_counter()
            observations, _info = environment.reset(
                match_config=MatchConfig(deck0=deck, deck1=deck, seed=90210 + episode * 1000 + env_id),
                options={
                    "render_mode": "headless",
                    "decision_ticks": POLICY_DECISION_TICKS,
                    "event_driven_decisions": False,
                    "allow_initial_remaining_runtime_rejections": True,
                },
            )
            initial_ticks[env_id] = int(observations[0].tick)
            print(
                f"[episode {episode}] lane {env_id} reset in {time.perf_counter() - started:.2f}s "
                f"-> tick {initial_ticks[env_id]}",
                flush=True,
            )

        executor = ThreadPoolExecutor(max_workers=args.lanes, thread_name_prefix="resident-smoke")
        wait_commands = {owner: (ActionV1.wait(owner, ticks=POLICY_DECISION_TICKS),) for owner in (0, 1)}
        active = list(range(args.lanes))
        final_ticks = dict(initial_ticks)
        rounds = 0
        started = time.perf_counter()
        coordinator.start_session()
        try:
            while active:
                coordinator.begin_round(tuple(active))
                futures = {
                    env_id: executor.submit(environments[env_id].step, wait_commands, record_trace=False)
                    for env_id in active
                }
                results: dict[int, tuple] = {}
                errors: dict[int, BaseException] = {}
                for env_id, future in futures.items():
                    step_started = time.perf_counter()
                    try:
                        results[env_id] = future.result()
                    except BaseException as error:  # noqa: BLE001 - report and drop the lane
                        errors[env_id] = error
                    finally:
                        timing["step_s"] += time.perf_counter() - step_started
                        timing["step_calls"] += 1
                native_ok = coordinator.round_succeeded
                finish_error: BaseException | None = None
                try:
                    coordinator.finish_round()
                except BaseException as error:  # noqa: BLE001
                    finish_error = error
                if not native_ok or finish_error is not None:
                    raise (finish_error or next(iter(errors.values())) or RuntimeError("batch round failed"))
                for env_id in active[:]:
                    if env_id in errors:
                        print(f"[episode {episode}] lane {env_id} FAILED: {errors[env_id]!r}", flush=True)
                        active.remove(env_id)
                        continue
                    observations, _rewards, terms, truncs, _infos = results[env_id]
                    final_ticks[env_id] = int(observations[0].tick)
                    if bool(terms["__all__"]) or bool(truncs["__all__"]) or final_ticks[env_id] >= args.max_ticks:
                        reason = "terminal" if terms["__all__"] else "truncated" if truncs["__all__"] else "tick-cap"
                        crowns = {p.owner: int(p.crowns) for p in observations[0].players}
                        print(
                            f"[episode {episode}] lane {env_id} done ({reason}) at tick "
                            f"{final_ticks[env_id]} crowns={crowns}",
                            flush=True,
                        )
                        active.remove(env_id)
                rounds += 1
        finally:
            executor.shutdown(wait=True)
            coordinator.close_session()
        elapsed = time.perf_counter() - started
        stepped = {env_id: final_ticks[env_id] - initial_ticks[env_id] for env_id in range(args.lanes)}
        step_wall = timing["step_s"] / max(timing["step_calls"], 1) * 1000.0
        observe_wall = timing["observe_s"] / max(timing["observe_calls"], 1) * 1000.0
        print(
            json.dumps(
                {
                    "episode": episode,
                    "rounds": rounds,
                    "elapsed_s": round(elapsed, 2),
                    "stepped_ticks": stepped,
                    "aggregate_tps": round(sum(stepped.values()) / elapsed, 1),
                    "step_wall_ms_sum_over_lanes": round(timing["step_s"] * 1000.0, 0),
                    "step_wall_ms_avg_per_call": round(step_wall, 1),
                    "observe_ms_avg_per_call": round(observe_wall, 1),
                    "observe_share_of_step": round(timing["observe_s"] / max(timing["step_s"], 1e-9), 3),
                }
            ),
            flush=True,
        )
        return stepped

    print("before:", json.dumps(natives[0].resident_status()), flush=True)
    totals: dict[int, int] = {env_id: 0 for env_id in range(args.lanes)}
    overall = time.perf_counter()
    for episode in range(args.episodes):
        stepped = run_episode(episode)
        for env_id, ticks in stepped.items():
            totals[env_id] += ticks
    elapsed = time.perf_counter() - overall
    print("after:", json.dumps(natives[0].resident_status()), flush=True)
    profile = coordinator.profile()
    print(
        json.dumps(
            {
                "total_stepped_ticks": totals,
                "episodes": args.episodes,
                "elapsed_s": round(elapsed, 2),
                "aggregate_tps_incl_reset": round(sum(totals.values()) / elapsed, 1),
                "channel_profile": {
                    key: (round(value / 1e6, 1) if key.endswith("_ns") and isinstance(value, int) else value)
                    for key, value in profile.items()
                },
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
