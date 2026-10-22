"""Replay a recorded slim trajectory through the local native renderer.

Loads a trajectory.jsonl (one line per decision step: tick + actions), drives
the native-render battle by injecting each step's non-wait actions as native
deploy commands when the game clock reaches the step tick, and optionally
starts/stops adb screenrecord around the replay.

Usage:
    python3 replay_driver.py --trajectory replay-match.jsonl --match-json render-match37.json \
        --record /sdcard/demo.mp4 [--speed 1.0]
"""

import argparse
import json
import socket
import subprocess
import sys
import time

ADB = "/Users/gujiga/Library/Android/sdk/platform-tools/adb"
PORT = 26789
PACKAGE = "nullsroyale.rel.free"
ENGINE_COMPONENT = f"{PACKAGE}/com.supercell.clashroyale.Engine0App"
TICK_MS = 50.0


def control(command, timeout=15.0):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
    s.settimeout(timeout)
    s.sendall((command + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    raw = buf.decode("utf-8").strip()
    if not raw:
        raise RuntimeError(f"empty control response for {command[:40]!r}")
    return json.loads(raw)


def adb(*args):
    return subprocess.run([ADB] + list(args), capture_output=True, timeout=120)


def restart_engine():
    """Fresh engine with window focus; native-render crashes at finalization."""
    adb("shell", "am", "force-stop", PACKAGE)
    time.sleep(1.5)
    adb("shell", "am", "start", "--user", "0", "-f", "0x18000000", "-n", ENGINE_COMPONENT)
    adb("forward", "tcp:26789", "tcp:26789")
    for _ in range(60):
        time.sleep(5)
        try:
            attested = control("attest", timeout=8.0).get("attestation", {})
            if attested.get("production_ready") is True:
                print("engine production-ready")
                return
        except Exception:  # noqa: BLE001 - still cold
            continue
    raise SystemExit("engine did not become production-ready")


def read_deal():
    """Read both owners' opening hand and draw order from the configured match.

    The native player object exposes ``hand`` (four cards with handIndex) and
    ``cycle`` (the remaining four in draw order), which is the whole deal: the
    renderer refills a played slot from the head of the cycle and pushes the
    played card onto its tail, so the hand is fully predictable from here on.
    """

    observed = control("observe", timeout=30.0)
    players = observed.get("players") if isinstance(observed, dict) else None
    if not players:
        return {}, int(observed.get("tick", -1)) if isinstance(observed, dict) else -1
    deals = {}
    for player in players:
        hand = [int(card["cardId"]) for card in (player.get("hand") or [])]
        cycle = [int(card["cardId"]) for card in (player.get("cycle") or [])]
        if len(hand) == 4 and len(cycle) == 4:
            deals[str(int(player.get("owner", -1)))] = {"hand": hand, "cycle": cycle}
    return deals, int(observed.get("tick", -1))


def plan_plays_with_deal(steps, deals, *, max_tick):
    """Place every recorded card on a tick where the actor actually holds it.

    The recorded tick is the earliest acceptable moment; a card that is still in
    the cycle is deferred until the hand refill that brings it back, keeping the
    model's card and placement instead of dropping the play.  Order within an
    owner is preserved.  Returns (plays, deferred) where a play is
    (owner, card_id, cell_x, cell_y, execute_tick).
    """

    planned = {"0": [], "1": []}
    for step in steps:
        if int(step["tick"]) > max_tick:
            continue
        for owner_key in ("0", "1"):
            for action in step["actions"].get(owner_key, ()):
                if action.get("kind") == "activate_ability":
                    raise ValueError("skill events require source-aware replay; card-only retiming would drop them")
                if action.get("kind") != "play_card":
                    continue
                x, y = action.get("target_grid", (0, 0))
                planned[owner_key].append(
                    {
                        "tick": int(step["tick"]),
                        "card": int(action.get("card_id", 0)),
                        "cell": (int(x), int(y)),
                        "offset": max(2, int(action.get("execute_offset_ticks", 2))),
                    }
                )
    plays: list[tuple[str, int, int, int, int]] = []
    deferred = {"0": 0, "1": 0}
    for owner_key in ("0", "1"):
        state = deals.get(owner_key)
        if state is None:
            for item in planned[owner_key]:
                plays.append((owner_key, item["card"], item["cell"][0], item["cell"][1], item["tick"] + item["offset"]))
            continue
        hand = list(state["hand"])
        queue = list(state["cycle"])
        pending: list[dict] = []
        last_tick = 0
        for item in planned[owner_key]:
            pending.append(item)
            while True:
                index = next((i for i, candidate in enumerate(pending) if candidate["card"] in hand), None)
                if index is None:
                    break
                pick = pending.pop(index)
                execute_tick = max(pick["tick"] + pick["offset"], last_tick + 1)
                last_tick = execute_tick
                slot = hand.index(pick["card"])
                hand[slot] = queue.pop(0)
                queue.append(pick["card"])
                plays.append((owner_key, pick["card"], pick["cell"][0], pick["cell"][1], execute_tick))
        deferred[owner_key] = len(pending)
    return plays, deferred


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--match-json", required=True)
    parser.add_argument("--record", default="")
    parser.add_argument("--record-limit", type=int, default=400, help="screenrecord natural time limit (seconds)")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--max-tick", type=int, default=6200)
    args = parser.parse_args()

    payload = open(args.match_json).read().strip()
    steps = [json.loads(line) for line in open(args.trajectory)]

    restart_engine()
    print("configuring native-render match ...")
    result = control(f"configure-native {payload}", timeout=60.0)
    if not result.get("ok"):
        raise SystemExit(f"configure-native failed: {result}")
    print("configured, sequence", result.get("sequence"))

    print("waiting for controller active ...")
    for _ in range(180):
        status = control("status")
        if status.get("nativeRenderReady") and int(status.get("nativeRenderLoaded", 0)) >= int(
            result.get("sequence", 1)
        ):
            print("controller active at tick", status.get("tick"))
            break
        time.sleep(1.0)
    else:
        raise SystemExit("native renderer never became ready")

    deals, deal_tick = read_deal()
    print("observed deal at tick", deal_tick, "owners", sorted(deals), flush=True)

    control("render on")
    if args.speed != 1.0:
        control(f"speed {args.speed}")
        print("speed set to", args.speed)

    # Schedule every recorded card play, moved to a tick where the actor really
    # holds that card (see plan_plays_with_deal).  The probe resolves the hand
    # slot on the exact pre-step world, so a play only lands when the card is in
    # hand; scheduling the raw tick drops it and the renderer never re-decides.
    #
    # Native world coordinates are 1000 units per tile (probe tower constants
    # put the King at x=9000 and the towers at y=3000/6500, and arena.py's
    # cell_to_world uses 1000), and the scheduled-card path forwards px/py to
    # the game command unscaled.  Sending half-scale positions silently loses
    # every play whose placement falls outside the deployable zone.
    CELL_UNITS = 1000
    plays, deferred = plan_plays_with_deal(steps, deals, max_tick=args.max_tick)
    scheduled = 0
    sequences = []
    sequence_owners = []
    for owner, card_id, x, y, execute_tick in plays:
        world_x = int(x) * CELL_UNITS + CELL_UNITS // 2
        world_y = int(y) * CELL_UNITS + CELL_UNITS // 2
        response = control(
            f"replay-schedule-card {owner} {card_id} {world_x} {world_y} {execute_tick}", timeout=8.0
        )
        if response.get("ok") is not True:
            raise SystemExit(f"schedule failed at tick {execute_tick}: {response.get('error', response)}")
        scheduled += 1
        sequences.append(int(response.get("sequence", 0)))
        sequence_owners.append(owner)
    print(
        f"scheduled {scheduled} card plays (deferred-but-unplaced {deferred})",
        flush=True,
    )

    recorder = None
    if args.record:
        adb("shell", "rm", "-f", args.record)
        recorder = subprocess.Popen(
            [ADB, "shell", "screenrecord", "--time-limit", str(args.record_limit), args.record],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("screenrecord started:", args.record, "limit", args.record_limit)

    # Monitor until the cutoff tick or match finalization.  The native clock
    # stops once the match ends, so waiting for the cutoff alone hangs forever
    # when the replayed match concludes earlier than the recording window.
    started = time.perf_counter()
    stop_reason = "max-tick"
    try:
        final_tick = -1
        while True:
            try:
                status = control("status")
            except Exception as error:  # noqa: BLE001 - the native renderer dies at finalization
                stop_reason = f"engine-lost: {type(error).__name__}"
                print("engine stopped responding; reporting the schedule anyway", flush=True)
                break
            tick = int(status.get("tick", -1))
            if tick < 0:
                time.sleep(0.1)
                continue
            final_tick = tick
            if status.get("ended") or status.get("finalized"):
                stop_reason = "match-ended"
                break
            if tick >= args.max_tick:
                break
            time.sleep(0.5)
        elapsed = time.perf_counter() - started
        tally = {"succeeded": 0, "failed": 0, "pending": 0}
        failures = {}
        for sequence in sequences:
            try:
                result = control(f"replay-schedule-status {sequence}", timeout=8.0)
            except Exception as error:  # noqa: BLE001
                tally["failed"] += 1
                failures[f"transport: {error}"[:100]] = failures.get(f"transport: {error}"[:100], 0) + 1
                continue
            state = str(result.get("state", "unknown"))
            tally[state] = tally.get(state, 0) + 1
            if state == "failed":
                key = f"{result.get('error', '?')} card={result.get('cardId')}"[:120]
                failures[key] = failures.get(key, 0) + 1
        owner_by_card = {}
        for owner_key, deck_key in (("0", "deck0"), ("1", "deck1")):
            for entry in (json.loads(payload).get("battle", {}).get(deck_key, {}) or {}).get("sp", []) or []:
                owner_by_card[int(entry["d"])] = owner_key
        per_owner = {
            owner_key: {"scheduled": 0, "succeeded": 0, "failed": 0, "pending": 0}
            for owner_key in ("0", "1")
        }
        for owner_key, _card, _x, _y, _tick in plays:
            per_owner[owner_key]["scheduled"] += 1
        for sequence, owner_key in zip(sequences, sequence_owners):
            try:
                state = str(control(f"replay-schedule-status {sequence}", timeout=8.0).get("state", "unknown"))
            except Exception:  # noqa: BLE001
                state = "unknown"
            if state == "succeeded":
                per_owner[owner_key]["succeeded"] += 1
            elif state == "pending":
                per_owner[owner_key]["pending"] += 1
            else:
                per_owner[owner_key]["failed"] += 1
        print(
            json.dumps(
                {
                    "scheduled_plays": scheduled,
                    "deferred_but_unplaced": deferred,
                    "schedule_outcome": tally,
                    "per_owner": per_owner,
                    "failure_reasons": failures,
                    "stop_reason": stop_reason,
                    "elapsed_s": round(elapsed, 1),
                    "final_tick": final_tick,
                }
            ),
            flush=True,
        )
    finally:
        if recorder is not None:
            # Let screenrecord hit its natural time limit: that is the only
            # path that reliably writes the MP4 moov atom on this emulator.
            try:
                recorder.wait(timeout=args.record_limit + 45)
            except subprocess.TimeoutExpired:
                subprocess.run([ADB, "shell", "kill -2 $(pidof screenrecord)"], capture_output=True, timeout=30)
                recorder.kill()
            print("screenrecord finalized")
        adb("shell", "am", "force-stop", PACKAGE)
        print("engine stopped before finalization")


if __name__ == "__main__":
    main()
