"""Freeze commands recorded by the pinned native selection builder.

This consumes a successful diagnostic recording, never a benchmark. A command
being queued does not prove application; run the frozen replay independently
and check observed state and paired original-serializer archives afterwards.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from run_headless_match import verify_completion


PREFIX = "HEADLESS_BATTLE_DEPLOY_QUEUED "
RECORD = re.compile(
    r"^HEADLESS_BATTLE_DEPLOY_QUEUED tick=(\d+) owner=([01]) slot=(\d+) "
    r"card=(\d+) packed=(\d+) cost=(\d+) elixir=(\d+) "
    r"queue_before=(\d+) queue_after=(\d+) json=(\{.*\})$")


def freeze(source, stdout, exit_code, plan):
    """Return a replay and evidence after validating raw source/log identities."""
    final_tick = verify_completion(exit_code, stdout)
    source_sha = hashlib.sha256(source).hexdigest()
    command = plan["command"]
    env = plan["environment"]
    if (plan.get("record_hand_actions") is not True
            or plan.get("benchmark_repeats") is not None
            or env.get("NASHURA_DEPLOY_HAND") != "1"
            or "NASHURA_BENCHMARK_MINIMAL" in env
            or command[-1] != source_sha
            or not command[-2].startswith("/oldroot/")
            or plan["identities"].get(command[-2][len("/oldroot"):]) != source_sha):
        raise ValueError("recording plan or replay identity mismatch")
    replay = json.loads(source)
    if any(replay.get(name) != [] for name in ("cmd", "inject", "evt")):
        raise ValueError("recording source must have empty command/event arrays")
    decks = [[entry["d"] for entry in replay["battle"][f"deck{owner}"]["sp"]]
             for owner in (0, 1)]
    if any(len(deck) != 8 or len(set(deck)) != 8 for deck in decks):
        raise ValueError("two complete distinct-card eight-card decks required")
    actions, records = [], []
    previous_tick, seen = -1, set()
    for line in stdout.splitlines():
        if not line.startswith(PREFIX):
            continue
        match = RECORD.fullmatch(line)
        if match is None:
            raise ValueError("malformed native action recording")
        tick, owner, slot, card, packed, cost, elixir, before, after = map(int, match.groups()[:9])
        action = json.loads(match[10])
        c = action.get("c", {})
        sel = c.get("sel", {})
        if (not previous_tick <= tick < final_tick or (tick, owner) in seen
                or not 0 <= slot < 8 or card not in decks[owner]
                or not 0 <= packed < 2**32 or after != before + 1
                or ((packed >> 22) & 63) - 1 != slot
                or packed >> 28 != cost or elixir < cost * 10000
                or action.get("ct") != 86 or c.get("t") != tick
                or c.get("t2") != tick + 20 or sel.get("os") != card
                or sel.get("pd") != packed):
            raise ValueError("inconsistent native action recording")
        previous_tick = tick
        seen.add((tick, owner))
        actions.append(action)
        records.append(dict(tick=tick, owner=owner, slot=slot, card=card,
                            packed=packed, cost=cost, elixir=elixir))
    if not actions or {row["owner"] for row in records} != {0, 1}:
        raise ValueError("nonempty native actions for both players required")
    fixed = deepcopy(replay)
    fixed["cmd"] = actions
    return fixed, dict(schema="nashura.headless-frozen-actions.v1",
                       source_sha256=source_sha,
                       stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                       recorded_final_tick=final_tick, seed=replay["rndSeed"],
                       decks=decks, command_count=len(actions), records=records,
                       whole_goal_complete=False,
                       scope="native selection and queue acceptance only; application and serializer equivalence require separate runs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args()
    if args.output == args.summary or args.output.exists() or args.summary.exists():
        raise ValueError("distinct new output and summary paths required")
    fixed, summary = freeze(args.source.read_bytes(),
                            (args.recording / "stdout.log").read_text(),
                            int((args.recording / "exit.txt").read_text()),
                            json.loads((args.recording / "plan.json").read_text()))
    encoded = (json.dumps(fixed, separators=(",", ":")) + "\n").encode()
    if not 2 <= len(encoded) < 65536:
        raise ValueError("frozen replay exceeds runner size contract")
    summary["frozen_replay_sha256"] = hashlib.sha256(encoded).hexdigest()
    with args.output.open("xb") as target:
        target.write(encoded)
    with args.summary.open("x") as target:
        json.dump(summary, target, indent=2)
        target.write("\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}))


if __name__ == "__main__":
    main()
