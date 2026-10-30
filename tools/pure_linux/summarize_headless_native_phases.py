"""Validate diagnostic call-site attribution; never label it benchmark timing."""
import argparse
import hashlib
import json
from pathlib import Path

from run_headless_match import verify_completion
from summarize_headless_benchmark import records

PHASES = ("game_state_update", "world_update", "object_collection_tick",
          "command_queue", "command_dispatch_mode2", "command_dispatch_mode3")


def summarize(text):
    tick = verify_completion(0, text)
    lines = text.splitlines()
    marker = "HEADLESS_NATIVE_PHASE_INSTALL installed=1 sites=8 scope=diagnostic-only"
    if lines.count(marker) != 1 or any(line.startswith(("HEADLESS_NATIVE_OPTIMIZATION ",
            "HEADLESS_NATIVE_PHASE_INSTALL_ERROR ", "HEADLESS_BENCHMARK_METRICS ")) for line in lines):
        raise ValueError("missing unique diagnostic installation or optimization mixed in")
    rows = records(text, "HEADLESS_NATIVE_PHASE")
    if len(rows) != len(PHASES) or [row.get("phase") for row in rows] != list(PHASES):
        raise ValueError("incomplete or duplicate phase records")
    result = {}
    fields = ("calls", "inclusive_ns", "exclusive_ns", "min_ns", "max_ns",
              "zero_results", "zero_duration_calls", "different_target_calls")
    for row in rows:
        if row.get("scope") != "diagnostic-only" or row.get("valid") != "1":
            raise ValueError("invalid diagnostic counter state")
        values = {key: int(row[key]) for key in fields}
        if any(value < 0 for value in values.values()):
            raise ValueError("negative native phase counter")
        target = int(row["first_target_offset"], 16)
        count = values["calls"]
        if not 0 <= values["exclusive_ns"] <= values["inclusive_ns"]:
            raise ValueError("invalid inclusive/exclusive accounting")
        if count:
            if (not target or not 0 <= values["min_ns"] <= values["max_ns"]
                    or not count * values["min_ns"] <= values["inclusive_ns"] <= count * values["max_ns"]
                    or values["zero_duration_calls"] > count
                    or (values["min_ns"] == 0) != (values["zero_duration_calls"] > 0)
                    or values["zero_results"] > count or values["different_target_calls"] >= count):
                raise ValueError("inconsistent populated phase counters")
        elif target or any(values.values()):
            raise ValueError("nonzero data for an uncalled phase")
        values["first_target_offset"] = row["first_target_offset"]
        values["mean_inclusive_ns"] = values["inclusive_ns"] / count if count else None
        result[row["phase"]] = values
    if result["game_state_update"]["calls"] != tick:
        raise ValueError("state-update count does not match completed ticks")
    for name, expected in (("game_state_update", 0x1062274),
                           ("world_update", 0xcef550), ("command_queue", 0xd2f37c)):
        row = result[name]
        if row["calls"] and (int(row["first_target_offset"], 16) != expected or row["different_target_calls"]):
            raise ValueError("wrong direct native target")
    state, world, objects, queue, mode2, mode3 = (result[name] for name in PHASES)
    if (state["inclusive_ns"] - state["exclusive_ns"] != world["inclusive_ns"] + queue["inclusive_ns"]
            or world["inclusive_ns"] - world["exclusive_ns"] != objects["inclusive_ns"]
            or queue["inclusive_ns"] - queue["exclusive_ns"] != mode2["inclusive_ns"] + mode3["inclusive_ns"]
            or objects["inclusive_ns"] != objects["exclusive_ns"]
            or mode2["inclusive_ns"] != mode2["exclusive_ns"]
            or mode3["inclusive_ns"] != mode3["exclusive_ns"]):
        raise ValueError("inconsistent native phase nesting")
    phase_rows = records(text, "HEADLESS_PHASE_TIMING")
    if len(phase_rows) != 1 or phase_rows[0].get("scope") != "diagnostic-only":
        raise ValueError("missing unique parse/enqueue diagnostic record")
    phases = {key: int(value) for key, value in phase_rows[0].items() if key != "scope"}
    if any(value < 0 for key, value in phases.items() if key not in ("first_queue_change_tick", "last_queue_change_tick")):
        raise ValueError("negative parse/enqueue counter")
    total_commands = phases["loaded_commands"] + phases["injected_commands"]
    if (total_commands <= 0 or phases["queue_at_step_start"] != total_commands
            or phases["queue_at_step_start"] + phases["queue_added"] - phases["queue_removed"] != phases["queue_at_end"]):
        raise ValueError("command queue conservation failed")
    dispatches = sum(result[name]["calls"] for name in PHASES[-2:])
    if dispatches > phases["queue_removed"]:
        raise ValueError("dispatch calls exceed removed commands")
    return {"schema": "nashura.headless-native-phases.v1", "scope": "diagnostic-only",
            "final_tick": tick, "phases": result, "parse_enqueue": phases,
            "dispatch_calls": dispatches,
            "zero_dispatch_results": sum(result[name]["zero_results"] for name in PHASES[-2:]),
            "benchmark": False, "whole_goal_complete": False,
            "limitations": ["counter and clock overhead are included; no speed comparison",
                "nested inclusive times overlap; exclusive removes only instrumented child windows",
                "dispatch result zero is raw native status, not proof of action success",
                "zero-duration calls are below clock resolution, not free operations",
                "only pinned call sites and fixture paths are covered"],
            "stdout_sha256": hashlib.sha256(text.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(args.log.read_text())
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
