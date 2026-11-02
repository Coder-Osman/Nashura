"""Reconfigure dedicated test slot0; hash existing captures without extra native RPCs."""

import argparse
import hashlib
from copy import deepcopy
from pathlib import Path
from native_runner.cr_native_env import JSON_EVENT_RINGS
import json
import inspect
import sys

from native_runner.cr_native_env import ResidentNativeClashEnv
from native_runner.match_factory import (
    MatchConfig,
    NATIVE_FINALIZATION_DEADLINE_TICK,
    tournament_level_kwargs,
)
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.sspi import collector
from native_runner.training.sspi.collector import EventBatchedSSPINative, collect_episode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", default="sspi-overhead-benchmark-20261102")
    parser.add_argument("--game-id", default="BENCH-0001")
    parser.add_argument("--stream-id", default="sspi-overhead-benchmark-pair-0001")
    parser.add_argument("--diagnose-terminal", action="store_true")
    parser.add_argument("--transport", choices=("event-json", "event-json-delta"), default="event-json-delta")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("preserve existing capture evidence; use a new output directory")

    native = ResidentNativeClashEnv(0, "127.0.0.1", args.port, timeout=90.)
    before = native.resident_status()
    slots = {item["envId"]: item["tick"] for item in before["slots"]}
    if any(item["envId"] != 0 or not item["ended"] for item in before["slots"]):
        raise RuntimeError(f"released resident slots unexpectedly occupied: {slots}")
    native.create_match(MatchConfig(
        deck0=HOG_DECK, deck1=HOG_DECK, seed=20261103,
        end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()))
    native.close_transport()

    original_observe = EventBatchedSSPINative.observe_atomic
    floors, bases, rows = {}, {}, []
    last = [None]
    deployment_base = [None]
    combat_records = []
    def normalize(value):
        if isinstance(value, dict):
            return {k: (0 if k in ("generation", "stateEpoch") else normalize(v)) for k, v in value.items()}
        if isinstance(value, list): return [normalize(v) for v in value]
        return value
    def digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
    def audited_observe(self):
        capture=original_observe(self)
        key=(capture['generation'],capture['stateEpoch'],capture['tick'],
            tuple(capture['rich'][name]['nextSequence'] for name in JSON_EVENT_RINGS))
        if key==last[0]: return capture
        last[0]=key
        current=normalize(capture)
        events={}
        for name in JSON_EVENT_RINGS:
            ring=current['rich'].pop(name)
            base=bases.setdefault(name,ring['epochFirstSequence'])
            floor=floors.get(name,ring['epochFirstSequence'])
            fresh=[e for e in ring['events'] if e['sequence']>=floor]
            for event in fresh:
                deployment=event.get('deploymentContext')
                if deployment is not None:
                    if deployment_base[0] is None:deployment_base[0]=deployment['deploymentSequence']
                    deployment['deploymentSequence']-=deployment_base[0]
                event['sequence']-=base
                if event.get('causeSequence') is not None:event['causeSequence']-=base
            for field in ['epochFirstSequence','oldestRetainedSequence','nextSequence','transmitFromSequence']:
                if field in ring:ring[field]-=base
            ring['transmitFromSequence']=floor-base
            ring['events']=fresh
            if name=='combatEvents':combat_records.append(dict(tick=capture['tick'],events=deepcopy(fresh)))
            events[name]=dict(sha256=digest(ring),new_events=len(fresh),next_sequence=ring['nextSequence'])
            floors[name]=capture['rich'][name]['nextSequence']
        rows.append(dict(tick=capture['tick'],current_sha256=digest(current),rings=events))
        return capture
    EventBatchedSSPINative.observe_atomic=audited_observe

    original_close = EventBatchedSSPINative.close_transport

    def close_with_profile(self):
        if self._atomic_steps:
            print(json.dumps({"stage": "capture_profile",
                "profile": self.finish_capture_profile()}, sort_keys=True), flush=True)
        return original_close(self)

    EventBatchedSSPINative.close_transport = close_with_profile

    if args.diagnose_terminal:
        source, start = inspect.getsourcelines(collector.collect_episode)
        terminal_line = next(start + offset for offset, line in enumerate(source)
            if 'raise ValueError("SSPI game did not reach a clean native terminal boundary")' in line)

        def trace_call(frame, event, _arg):
            if event != "call" or frame.f_code is not collector.collect_episode.__code__:
                return None

            def trace_line(inner_frame, inner_event, _inner_arg):
                if inner_event == "line" and inner_frame.f_lineno == terminal_line:
                    local = inner_frame.f_locals
                    runtime = local["rollout"].runtime
                    evidence = runtime.clock.evidence
                    print(json.dumps({"stage": "terminal_diagnostic",
                        "ended": runtime.ended, "truncated": runtime.truncated,
                        "failed": runtime.failed, "clock_tick": runtime.clock.tick,
                        "result_tick": local["result"]["terminal_tick"],
                        "pending": list(runtime.clock.pending),
                        "ready": list(runtime.clock.ready),
                        "frozen": list(runtime.frozen),
                        "rejected": [item for item in evidence if item["reason"] == "rejected"]},
                        sort_keys=True), flush=True)
                return trace_line

            return trace_line

        sys.settrace(trace_call)

    def progress(row):
        if row["stage"] != "decisions_committed" or row["tick"] % 1000 < 20:
            print(json.dumps(row, sort_keys=True), flush=True)

    try:
        result = collect_episode(
            actual_port=args.port, actual_env_id=0, belief_port=26790,
            belief_ports={0: 26790, 1: 26792}, output_dir=args.output,
            run_id=args.run_id, arm="A_5PCT_128",
            game_id=args.game_id, seed=20261103, device="cpu",
            transport=args.transport, episode_kind="evaluation",
            search_probability=0.0, temporal_augmentation=False,
            random_stream_id=args.stream_id,
            progress=progress)
        print(json.dumps({"stage": "complete", "elapsed_seconds": result["elapsed_seconds"],
            "terminal_tick": result["terminal_tick"],
            "native_advance_performance": result["native_advance_performance"]},
            sort_keys=True), flush=True)
    finally:
        sys.settrace(None)
        Path(args.output, "combat-audit.json").write_text(json.dumps(combat_records,sort_keys=True)+chr(10))
        Path(args.output, "boundary-audit.json").write_text(json.dumps(rows,sort_keys=True)+chr(10))
        print(json.dumps({"stage":"boundary_audit", "boundaries":len(rows)}),flush=True)
        check = ResidentNativeClashEnv(0, "127.0.0.1", args.port, timeout=90.)
        after = check.resident_status()
        check.close_transport()
        print(json.dumps({"stage": "resident_status", "slots": [
            {"env_id": item["envId"], "tick": item["tick"]}
            for item in after["slots"]]}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
