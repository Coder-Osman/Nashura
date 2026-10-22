"""Collect normal-touch/first-visible-spawn pairs on the dedicated live AVD."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .capture import DEFAULT_ADB
from .snapshot import LiveStateError, latest_snapshot
from .touch import deploy_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    points = ((200, 950), (880, 950), (200, 1390), (880, 1390))
    deadline = time.monotonic() + 140
    count = 0
    with args.output.open("x") as output:
        while count < len(points) and time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                row = latest_snapshot(args.capture)
            except LiveStateError:
                continue
            if row["ended"] or row["tick"] < 150:
                continue
            if time.monotonic_ns() - row["received_monotonic_ns"] > 500_000_000:
                continue
            options = [s for s in row["own"]["hand"] if s["card"]
                       and s["card"]["card_id"] in (26000000, 26000003, 26000014, 26000018)
                       and s["card"]["cost"] * 10000 <= row["own"]["elixir_raw"]]
            if not options:
                continue
            selected = min(options, key=lambda s: s["card"]["cost"])
            card_id = selected["card"]["card_id"]
            old_ids = {e["id"] for e in row["entities"]}
            receipt = args.output.with_name(args.output.stem + f"-touch-{count}.jsonl")
            deploy_once(adb=DEFAULT_ADB, serial=args.serial, capture=args.capture, receipt=receipt,
                        epoch=row["battle_epoch"], slot=selected["slot"], card_id=card_id, screen=points[count])
            stop = time.monotonic() + 4
            while time.monotonic() < stop:
                # The first spawn may precede hand replacement acknowledgement.
                with args.capture.open() as source:
                    for line in source:
                        try:
                            sample = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (sample.get("event") != "snapshot" or sample["battle_epoch"] != row["battle_epoch"]
                                or sample["tick"] <= row["tick"]):
                            continue
                        spawned = [e for e in sample["entities"] if e["id"] not in old_ids
                                   and e["owner"] == row["own_side"] and e["card_id"] == card_id]
                        if spawned:
                            if len(spawned) != 1:
                                raise LiveStateError("calibration requires a single visible spawn")
                            result = {"screen": points[count], "native": [spawned[0]["x"], spawned[0]["y"]],
                                      "owner": row["own_side"], "card_id": card_id,
                                      "tick": sample["tick"], "receipt": str(receipt)}
                            output.write(json.dumps(result) + "\n")
                            output.flush()
                            print(json.dumps(result), flush=True)
                            count += 1
                            break
                    else:
                        time.sleep(0.1)
                        continue
                break
            else:
                raise LiveStateError("touch acknowledged but first visible spawn was not captured")
        if count != len(points):
            raise LiveStateError(f"only collected {count} calibration points")


if __name__ == "__main__":
    main()
