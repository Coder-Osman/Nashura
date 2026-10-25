from dataclasses import asdict
import json
from pathlib import Path
import sys

import probe_trace
from plugin_audit import PluginAudit
from native_runner.training.fair_final.episode import atomic_json


def semantic_digest(raw):
    from hashlib import sha256

    return sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def main():
    measurements, reservations = [], []
    output = Path(sys.argv[sys.argv.index("--output") + 1])
    log = output.with_name("qemu.log")
    probe_trace.TraceAudit = PluginAudit

    def position():
        return log.stat().st_size if log.exists() else 0

    class MeasuredRollout(probe_trace.FinalRollout):
        def advance(self):
            environment = self.runtime.environment
            native = environment.native
            tick = environment.current_tick
            due = [asdict(plan) for plan in self.runtime.clock.pending.values()
                if plan.due_tick == tick + 1 and plan.action.card_id is not None]
            queued = []
            original = native.queue_hand_action_at
            start = position()
            native_tick = int(environment.raw_observation["tick"])

            def queue(action, **kwargs):
                plans = [plan for plan in due if plan["owner"] == action.owner]
                if len(plans) != 1 or any(row["reservation"] == plans[0]["identity"] for row in queued):
                    raise ValueError("reservation queue lacks one unique due owner")
                before = position()
                receipt = original(action, **kwargs)
                queued.append(dict(reservation=plans[0]["identity"], hand_action=asdict(action),
                    request=kwargs, receipt=receipt, log_start=before, log_end=position()))
                return receipt

            native.queue_hand_action_at = queue
            try:
                result = super().advance()
            finally:
                native.queue_hand_action_at = original
            end = position()
            if due or queued or end != start:
                reservations.append(dict(tick_before=tick, tick_after=environment.current_tick,
                    native_tick_before=native_tick, due=due, queues=queued,
                    outcomes={str(identity): reason for identity, reason in result.items()},
                    log_start=start, log_end=end))
            if environment.current_tick % 300 == 0 or self.runtime.ended:
                digest = native._request("state-digest")
                if digest.get("scope") != "native-payload+global-mt2504.v1":
                    raise ValueError("reservation measurement digest scope differs")
                measurements.append(dict(tick=environment.current_tick,
                    observation_sha256=semantic_digest(environment.raw_observation),
                    process_local_digest=digest))
            return result

    probe_trace.FinalRollout = MeasuredRollout
    probe_trace.main()
    atomic_json(output.with_name("measurements.json"), dict(schema="FairFinalRuntimeMeasurements/v1",
        records=measurements, reservation_schema="FairFinalReservationIntervals/v1",
        reservation_records=reservations, cross_process_native_payload_equality_claimed=False,
        training_admissible=False, gates_closed=False))


if __name__ == "__main__":
    main()
