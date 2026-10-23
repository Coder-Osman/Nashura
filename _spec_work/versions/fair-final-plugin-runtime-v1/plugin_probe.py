import hashlib
import json
from pathlib import Path

import probe_trace
from plugin_audit import PluginAudit
from native_runner.training.fair_final.episode import atomic_json


def semantic_digest(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def main():
    measurements = []
    probe_trace.TraceAudit = PluginAudit

    class MeasuredRollout(probe_trace.FinalRollout):
        def advance(self):
            result = super().advance()
            environment = self.runtime.environment
            tick = environment.current_tick
            if tick % 300 == 0 or self.runtime.ended:
                digest = environment.native._request("state-digest")
                if digest.get("scope") != "native-payload+global-mt2504.v1":
                    raise ValueError("plugin measurement digest scope differs")
                measurements.append(dict(tick=tick, observation_sha256=semantic_digest(environment.raw_observation),
                    process_local_digest=digest))
            return result

    probe_trace.FinalRollout = MeasuredRollout
    probe_trace.main()
    import sys
    output = Path(sys.argv[sys.argv.index("--output") + 1])
    atomic_json(output.with_name("measurements.json"), dict(schema="FairFinalRuntimeMeasurements/v1",
        records=measurements, cross_process_native_payload_equality_claimed=False,
        training_admissible=False, gates_closed=False))


if __name__ == "__main__":
    main()
