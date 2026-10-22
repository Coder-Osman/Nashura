import json
import os
import time

import puct_vs_policy_eval as evaluation
from native_runner import battle_env


names = (
    "_atomic_observation_pair",
    "_rich_context",
    "_consume_native_public_card_plays",
    "_consume_native_phase_events",
    "_consume_native_visibility_events",
    "_consume_native_remaining_events",
    "_consume_native_combat_events",
    "_update_beliefs",
    "_resolve_pending",
    "_append_native_public_card_plays",
    "_resolve_pending_abilities",
    "_derive_events",
    "_extract_towers",
    "_terminal",
    "_rewards",
    "observe_dual",
    "action_outcomes",
    "deployment_timing_events",
)
timings = {name: 0.0 for name in names}
calls = {name: 0 for name in names}


def wrap(name, original):
    def measured(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timings[name] += time.perf_counter() - started
            calls[name] += 1
    return measured


for name in names:
    original = getattr(battle_env.BattleEnvV1, name, None)
    if original is not None:
        setattr(battle_env.BattleEnvV1, name, wrap(name, original))


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
result["battle_step_probe"] = {
    name: {"seconds": timings[name], "calls": calls[name]}
    for name in names
    if calls[name]
}
path = os.environ["PROBE_OUTPUT"]
temporary = path + ".tmp"
with open(temporary, "w") as stream:
    json.dump(result, stream)
os.replace(temporary, path)
print(json.dumps(result["battle_step_probe"], sort_keys=True))
