import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import struct
import time

import torch

from combat_protocol import choose, mask_record
from probe_snapshot import SnapshotGlobalAudit
from trace_protocol import check_base, sha
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.transport import create_search_native
from native_runner.training.v4.factory import production_semantic_bundle
from seed_audit import mapped_base


MATCH_SEED = 78102011
EXECUTION_SEED = 78102021


class TraceAudit(SnapshotGlobalAudit):
    def addresses(self):
        with self._paused() as process:
            maps = (process / "maps").read_text()
            probe_base = mapped_base(maps, self.probe)
            descriptor = os.open(process / "mem", os.O_RDONLY)
            try:
                def symbol(name, format):
                    size = struct.calcsize(format)
                    content = os.pread(descriptor, size, probe_base + self.symbols[name])
                    if len(content) != size:
                        raise ValueError("short execution trace identity read")
                    return struct.unpack(format, content)[0]
                if symbol("worker", "<B"):
                    raise ValueError("execution trace lane is active")
                return dict(libg_base=symbol("libg", "<Q"), manager=symbol("manager", "<Q"),
                    tick=symbol("tick", "<i"), generation=symbol("generation", "<Q"),
                    stateEpoch=symbol("epoch", "<Q"), child_pid=self.pid,
                    child_start_ticks=self.record["child_start_ticks"], readonly=True)
            finally:
                os.close(descriptor)


def main():
    parser = argparse.ArgumentParser()
    for name in ("instance", "probe", "libg", "project", "puct-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", choices=("calibration", "trace"), required=True)
    parser.add_argument("--center-base", type=int)
    args = parser.parse_args()
    torch.set_num_threads(1)
    settings = json.loads(Path(__file__).with_name("settings.json").read_text())
    sources = json.loads((args.project / "probe-source-manifest.json").read_text())
    for name, expected in sources.items():
        if sha(args.project / name) != expected:
            raise ValueError("random execution frozen runtime differs")
    native = create_search_native(args.port, transport="compact-delta")
    started, decisions, boundaries, outcomes, executed, steps = time.monotonic(), [], [], [], 0, 0
    policy_closed_tick = None
    log = args.output.parent / "qemu.log"

    def position():
        return log.stat().st_size if log.exists() else 0

    try:
        audit = TraceAudit(args.instance, probe=args.probe, libg=args.libg, port=args.port,
            probe_sha=settings["engine_sha256"])
        before = audit.addresses()
        if args.mode == "trace":
            check_base(args.center_base, before["libg_base"])
        atomic_json(args.output.parent / "address-evidence.json", before)
        reset_offset = position()
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=MATCH_SEED,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        opening = audit.addresses()
        if opening["libg_base"] != before["libg_base"]:
            raise ValueError("guest library base changed during reset")
        battle_offset = position()
        rollout = FinalRollout(environment, catalog=production_semantic_bundle().card_catalog,
            execution_seed=EXECUTION_SEED)
        if args.mode == "trace":
            while not rollout.runtime.ended:
                if time.monotonic() - started > 900 or steps >= NATIVE_FINALIZATION_DEADLINE_TICK:
                    raise TimeoutError("bounded execution trace trajectory")
                if position() > 256 * 1024**2:
                    raise ValueError("bounded execution trace log exceeded 256MiB")
                information = rollout.freeze_due_information()
                actions = {owner: choose(info) for owner, info in information.items()}
                if information:
                    decisions.append(dict(tick=environment.current_tick, choices={str(owner):
                        dict(action=asdict(actions[owner]), mask=mask_record(info), fair_sha=info.sha256)
                        for owner, info in information.items()}))
                    rollout.commit_simultaneous(actions)
                result = rollout.advance()
                if any(value != "executed" for value in result.values()):
                    raise ValueError("legal execution trace reservation rejected")
                executed += len(result)
                if result:
                    outcomes.append(dict(tick=environment.current_tick, outcomes=result))
                steps += 1
                if not environment.policy_actions_open and policy_closed_tick is None:
                    policy_closed_tick = environment.current_tick
                if rollout.runtime.failed or rollout.runtime.truncated:
                    raise ValueError("execution trace failed or truncated")
                if steps % 300 == 0:
                    boundaries.append(dict(tick=environment.current_tick, log_offset=position()))
                    atomic_json(args.output.parent / "progress.json", dict(steps=steps,
                        tick=environment.current_tick, decision_groups=len(decisions), executed=executed,
                        trace_bytes=position(), seconds=time.monotonic() - started))
        final = audit.addresses()
        final_offset = position()
        terminal = environment.raw_observation if args.mode == "trace" else None
        atomic_json(args.output, dict(schema="FairFinalRandomExecutionProbe/v1", mode=args.mode,
            status="calibrated" if args.mode == "calibration" else "completed_pending_trace_verification",
            before=before, opening=opening, final=final, reset_offset=reset_offset,
            battle_offset=battle_offset, final_offset=final_offset, boundaries=boundaries,
            decisions=decisions, outcomes=outcomes, executed=executed, steps=steps, terminal=terminal,
            policy_closed_tick=policy_closed_tick, final_clock=rollout.runtime.clock.capture(),
            match_seed=MATCH_SEED, execution_seed=EXECUTION_SEED, engine_sha256=settings["engine_sha256"],
            source_sha256=sources, seconds=time.monotonic() - started,
            training_admissible=False, gates_closed=False))
    except BaseException as error:
        atomic_json(args.output.parent / "failure.json", dict(error=repr(error), steps=steps,
            decisions=len(decisions), training_admissible=False, gates_closed=False))
        raise
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
