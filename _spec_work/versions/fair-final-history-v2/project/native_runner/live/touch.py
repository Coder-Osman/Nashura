"""One guarded normal-client touch, acknowledged by native card consumption.

Screen coordinates must be inspected/calibrated for the active game viewport.
This diagnostic entry point does not accept model-space coordinates.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import threading
import time

from .capture import DEFAULT_ADB
from .snapshot import (CaptureReadError, LiveStateError, latest_snapshot,
                       same_battle_continuation, validate_touch)
from .input import PersistentInput
from .events import public_events


class TouchCancelled(LiveStateError):
    """No deployment touch was committed; the runner may decide again."""


class TouchEnded(LiveStateError):
    """The same battle ended while a committed touch awaited its receipt."""

    def __init__(self, after):
        super().__init__("battle ended without a matching native play receipt")
        self.after = after


class TouchTransaction:
    def __init__(self):
        self.lock = threading.Lock()
        self.reason = None
        self.committed = False

    def cancel(self, reason):
        with self.lock:
            if self.committed:
                return False
            self.reason = reason
            return True

    def check(self):
        if self.reason:
            raise TouchCancelled(self.reason)

    def commit(self, guard):
        with self.lock:
            self.check()
            row = guard()
            self.committed = True
            return row


class TouchSession:
    def __init__(self, *, adb, serial, capture, log_dir):
        self.adb, self.serial, self.capture = adb, serial, capture
        size = subprocess.check_output([str(adb), "-s", serial, "shell", "wm", "size"], text=True, timeout=10)
        sizes = re.findall(r"(?:Physical|Override) size: (\d+)x(\d+)", size)
        if not sizes or tuple(map(int, sizes[-1])) != (1080, 1920):
            raise LiveStateError("touch calibration requires the verified 1080x1920 viewport")
        with capture.open() as source:
            self.ready = json.loads(source.readline())
        pid = int(subprocess.check_output([str(adb), "-s", serial, "shell", "pidof", "nullsroyale.rel.free"],
                                         text=True, timeout=10))
        if self.ready.get("serial") != serial or self.ready.get("pid") != pid:
            raise LiveStateError("capture belongs to another device or process")
        self.transport = PersistentInput(adb, serial, log_dir=log_dir)

    def check_capture(self, capture):
        if capture != self.capture:
            raise LiveStateError("touch session capture changed")
        with capture.open() as source:
            if json.loads(source.readline()) != self.ready:
                raise LiveStateError("touch session capture identity changed")

    def close(self):
        self.transport.close()


def deploy_once(*, adb: Path, serial: str, capture: Path, receipt: Path,
                epoch: int, slot: int, card_id: int, screen: tuple[int, int],
                expected_size: tuple[int, int] = (1080, 1920), session: TouchSession | None = None,
                transaction: TouchTransaction | None = None, guard=None, decision_timing=None) -> dict:
    def command(*args: str) -> str:
        return subprocess.check_output([str(adb), "-s", serial, *args], text=True, timeout=10).strip()

    if session is None:
        size = command("shell", "wm", "size")
        sizes = re.findall(r"(?:Physical|Override) size: (\d+)x(\d+)", size)
        if not sizes or tuple(map(int, sizes[-1])) != expected_size or expected_size != (1080, 1920):
            raise LiveStateError("touch calibration requires the verified 1080x1920 viewport")
    else:
        session.check_capture(capture)
        if session.serial != serial:
            raise LiveStateError("touch session device changed")
    x, y = screen
    if not 80 <= x <= 1000 or not 150 <= y <= 1480:
        raise LiveStateError("deployment target outside calibrated arena bounds")
    with capture.open() as source:
        ready = json.loads(source.readline())
    if ready.get("serial") != serial or (session is None and ready.get("pid") != int(command("shell", "pidof", "nullsroyale.rel.free"))):
        raise LiveStateError("capture belongs to another device or process")
    transaction = transaction or TouchTransaction()

    def checked():
        transaction.check()
        row = latest_snapshot(capture)
        validate_touch(row, epoch=epoch, slot=slot, card_id=card_id)
        if guard is not None:
            guard(row)
        return row

    def tap(x, y):
        if session is not None:
            return session.transport.tap(x, y)
        command("shell", "input", "tap", str(x), str(y))

    before = checked()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents replaying an action whose outcome is uncertain.
    with receipt.open("x") as log:
        def record(event, **values):
            log.write(json.dumps({"event": event, "monotonic_ns": time.monotonic_ns(), **values}) + "\n")
            log.flush()

        record("requested", serial=serial, pid=ready["pid"], epoch=epoch, slot=slot,
               card_id=card_id, screen=screen, before=before, decision_timing=decision_timing)
        try:
            record("hand_touch_started", tick=before["tick"])
            hand_ms = tap((332, 537, 741, 945)[slot], 1700)
            hand_timing = getattr(session.transport, "last_timing", None) if session is not None else None
            record("hand_touch_completed", touch_ms=hand_ms, input_timing=hand_timing)
            current = transaction.commit(checked)
            selected_card = dict(current["own"]["hand"][slot]["card"])
            watermark = len(public_events(current, required=session is not None))
            record("target_touch_started", tick=current["tick"])
            target_ms = tap(x, y)
            target_timing = getattr(session.transport, "last_timing", None) if session is not None else None
            record("target_touch_completed", touch_ms=target_ms, input_timing=target_timing)
            deadline = time.monotonic() + 3
            observation_error = None
            epoch_rebound = False

            def observe_receipt():
                nonlocal observation_error
                try:
                    row = latest_snapshot(capture)
                except CaptureReadError as exc:
                    # Only observe again. The committed target is never resent,
                    # and a discarded sample never falls back to an older row.
                    message = str(exc)
                    if message != observation_error:
                        record("receipt_observation_wait", reason=message)
                    observation_error = message
                    return None
                nonlocal epoch_rebound
                if row["battle_epoch"] != epoch:
                    if not same_battle_continuation(current, row):
                        raise LiveStateError("battle changed before acknowledgement")
                    epoch_rebound = True
                age_ms = (time.monotonic_ns() - row["received_monotonic_ns"]) / 1e6
                if not 0 <= age_ms <= 500:
                    message = "receipt observation is older than 500 ms"
                    if message != observation_error:
                        record("receipt_observation_wait", reason=message)
                    observation_error = message
                    return None
                if observation_error is not None:
                    record("receipt_observation_resumed", tick=row["tick"])
                    observation_error = None
                return row

            sent = observe_receipt()
            record("touch_sent", tick=sent["tick"] if sent is not None else None)
            while time.monotonic() < deadline:
                time.sleep(0.03)
                after = observe_receipt()
                if after is None:
                    continue
                events = public_events(after, required=session is not None)
                native = next((event for event in events
                               if ((epoch_rebound and event["tick"] >= current["tick"])
                                   or (not epoch_rebound and event["sequence"] > watermark))
                               and event["owner"] == after["own_side"]
                               and event["card_id"] == card_id
                               and event["card_identity"] == "own-deck"), None)
                if native is not None and any(native[key] != selected_card[key]
                                              for key in ("form_code", "cost") if key in selected_card):
                    raise LiveStateError("native play receipt differs from selected form or cost")
                replacement = after["own"]["hand"][slot]["card"]
                rotated = replacement is not None and replacement["card_id"] != card_id
                if native is not None or (session is None and after["tick"] > current["tick"] and rotated):
                    result = {"event": "native_play_confirmed" if native is not None else "hand_rotation_confirmed",
                              "before_tick": before["tick"],
                              "target_touch_tick": current["tick"],
                              "touch_sent_tick": sent["tick"] if sent is not None else None,
                              "hand_touch_ms": hand_ms, "target_touch_ms": target_ms,
                              "native_receipt": native, "hand_rotated": rotated,
                              "after_tick": after["tick"], "after_epoch": after["battle_epoch"],
                              "epoch_rebound": epoch_rebound, "after": after}
                    record(**result)
                    return result
                if after["ended"]:
                    raise TouchEnded(after)
            if observation_error is not None:
                raise LiveStateError("receipt observation did not recover within 3 seconds: " + observation_error)
            raise LiveStateError("no matching native play receipt observed; no automatic retry"
                                 if session is not None else "no hand rotation observed; no automatic retry")
        except TouchEnded as exc:
            record("unconfirmed_at_terminal", reason=str(exc),
                   terminal={key: exc.after[key] for key in
                             ("battle_epoch", "tick", "ended", "winner", "own_side")})
            raise
        except Exception as exc:
            record("unconfirmed" if transaction.committed else "cancelled_before_target", reason=str(exc))
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--card-id", type=int, required=True)
    parser.add_argument("--screen", type=int, nargs=2, required=True)
    args = parser.parse_args()
    result = deploy_once(**{**vars(args), "screen": tuple(args.screen)})
    print(json.dumps({k: v for k, v in result.items() if k != "after"}))


if __name__ == "__main__":
    main()
