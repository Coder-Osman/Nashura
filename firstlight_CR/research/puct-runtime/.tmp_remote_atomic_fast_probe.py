import json
import os

import puct_vs_policy_eval as evaluation
from native_runner.cr_native_env import NativeClashEnv


original_validate = NativeClashEnv._validate_atomic_result


def raw_atomic_result(self, result):
    if getattr(self, "_probe_skip_atomic_transport_validation", False):
        return result
    return original_validate(self, result)


NativeClashEnv._validate_atomic_result = raw_atomic_result
NativeClashEnv._probe_skip_atomic_transport_validation = True

result = evaluation.run_match(
    port=int(os.environ["PROBE_PORT"]),
    device="cuda:0",
    seed=int(os.environ["PROBE_SEED"]),
    puct_owner=0,
    simulations=32,
    max_seconds=600,
    transport="resident-hybrid",
    resident_capture="compact",
    first_root_only=True,
    profile_setup=True,
)
result["probe_fast_atomic"] = True
path = os.environ["PROBE_OUTPUT"]
temporary = path + ".tmp"
with open(temporary, "w") as stream:
    json.dump(result, stream)
os.replace(temporary, path)
print(json.dumps({
    "selected_action": result["selected_action"],
    "search_seconds": result["search_seconds"],
    "fingerprint_match": result["fingerprint_match"],
}))
