import json
import os
import time

import puct_vs_policy_eval as evaluation
from native_runner import battle_env
from native_runner import cr_native_env


profile = {
    "native_request_seconds": 0.0,
    "native_request_calls": 0,
    "step_atomic_seconds": 0.0,
    "step_atomic_calls": 0,
    "step_atomic_compact_seconds": 0.0,
    "step_atomic_compact_calls": 0,
    "atomic_validate_seconds": 0.0,
    "atomic_validate_calls": 0,
    "battle_atomic_pair_seconds": 0.0,
    "battle_atomic_pair_calls": 0,
    "commands": {},
}


original_request = cr_native_env.NativeClashEnv._request
original_step_atomic = cr_native_env.NativeClashEnv.step_atomic
original_step_atomic_compact = cr_native_env.NativeClashEnv.step_atomic_compact
original_validate = cr_native_env.NativeClashEnv._validate_atomic_result
original_pair = battle_env.BattleEnvV1._atomic_observation_pair


def request(self, command, *args, **kwargs):
    started = time.perf_counter()
    try:
        return original_request(self, command, *args, **kwargs)
    finally:
        if "step-atomic" in command:
            profile["native_request_seconds"] += time.perf_counter() - started
            profile["native_request_calls"] += 1
            profile["commands"][command.split()[0]] = profile["commands"].get(command.split()[0], 0) + 1


def step_atomic(self, ticks=1):
    started = time.perf_counter()
    try:
        return original_step_atomic(self, ticks)
    finally:
        profile["step_atomic_seconds"] += time.perf_counter() - started
        profile["step_atomic_calls"] += 1


def step_atomic_compact(self, ticks=1):
    started = time.perf_counter()
    try:
        return original_step_atomic_compact(self, ticks)
    finally:
        profile["step_atomic_compact_seconds"] += time.perf_counter() - started
        profile["step_atomic_compact_calls"] += 1


def validate(self, result):
    started = time.perf_counter()
    try:
        return original_validate(self, result)
    finally:
        profile["atomic_validate_seconds"] += time.perf_counter() - started
        profile["atomic_validate_calls"] += 1


def pair(self, capture=None):
    started = time.perf_counter()
    try:
        return original_pair(self, capture)
    finally:
        profile["battle_atomic_pair_seconds"] += time.perf_counter() - started
        profile["battle_atomic_pair_calls"] += 1


cr_native_env.NativeClashEnv._request = request
cr_native_env.NativeClashEnv.step_atomic = step_atomic
cr_native_env.NativeClashEnv.step_atomic_compact = step_atomic_compact
cr_native_env.NativeClashEnv._validate_atomic_result = validate
battle_env.BattleEnvV1._atomic_observation_pair = pair

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
result["step_atomic_profile"] = profile
path = os.environ["PROBE_OUTPUT"]
temporary = path + ".tmp"
with open(temporary, "w") as stream:
    json.dump(result, stream)
os.replace(temporary, path)
print(json.dumps(profile, sort_keys=True))
