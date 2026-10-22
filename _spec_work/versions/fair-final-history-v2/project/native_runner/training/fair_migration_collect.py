from collections import Counter
import hashlib
import math
from pathlib import Path
import time

import torch

from .fair_final.rollout import FinalRollout
from .fair_final.native import FinalBattleEnv
from .fair_migration_data import MigrationRecorder, teacher_targets, validate_teacher
from .fair_search.contracts import semantic_digest


def collection_protocols(engine_sha256):
    project = Path(__file__).resolve().parents[2]
    groups = {
        "engine_protocol_sha": ("native_runner/training/fair_final/native.py", "native_runner/match_factory.py"),
        "action_protocol_sha": tuple("native_runner/training/fair_final/" + name + ".py"
            for name in ("adapter", "codec", "policy", "timing")),
        "fair_protocol_sha": tuple("native_runner/training/fair_final/" + name + ".py"
            for name in ("information", "visibility", "rollout")),
        "observation_contract_sha": ("native_runner/contracts.py", "native_runner/training/v4/tensors.py",
            "native_runner/training/v4/tensorizer.py", "native_runner/training/event_cache.py"),
    }
    values = {key: {relative: hashlib.sha256((project / relative).read_bytes()).hexdigest()
        for relative in files} for key, files in groups.items()}
    values["engine_protocol_sha"].update(engine_sha256=engine_sha256, timing=FinalBattleEnv.timing_protocol)
    return {key: semantic_digest(value) for key, value in values.items()}


def collect_migration_episode(environment, *, teacher, store, match_id, attempt_id, execution_seed,
        wall_seconds=1200., progress=None, rollout_factory=FinalRollout, monotonic=time.monotonic):
    if (type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds) or wall_seconds <= 0):
        raise ValueError("positive finite migration wall limit required")
    if store.manifest["round"] != 0:
        raise ValueError("initial A-versus-A collector requires round zero; supplementary controllers pending")
    validate_teacher(teacher, store.manifest["identity"])
    recorder = MigrationRecorder(store, match_id=match_id, attempt_id=attempt_id, teacher=teacher)
    rollout = None
    started = monotonic()
    timings = Counter()
    last_progress_tick = -100

    def notify(status, **extra):
        if progress is not None:
            progress(dict(status=status, match_id=match_id, attempt_id=attempt_id,
                tick=None if rollout is None else rollout.runtime.clock.tick, rows=len(recorder.rows),
                resolved=recorder.resolved, seconds=monotonic() - started, **extra))

    try:
        rollout = rollout_factory(environment, catalog=teacher.policy.backbone.catalog, execution_seed=execution_seed)
        with torch.inference_mode():
            while not rollout.runtime.ended and not rollout.runtime.truncated:
                if monotonic() - started > wall_seconds:
                    raise TimeoutError("complete migration episode wall limit exceeded")
                before = monotonic()
                frozen = rollout.freeze_due_information()
                timings["freeze_seconds"] += monotonic() - before
                if frozen:
                    before = monotonic()
                    selected = {owner: teacher_targets(teacher, info)[0] for owner, info in frozen.items()}
                    timings["inference_seconds"] += monotonic() - before
                    rollout.commit_simultaneous(selected)
                    before = monotonic()
                    recorder.record_committed(frozen, selected, rollout.runtime.clock)
                    timings["record_seconds"] += monotonic() - before
                before = monotonic()
                rollout.advance()
                recorder.reconcile(rollout.runtime.clock)
                timings["advance_seconds"] += monotonic() - before
                if rollout.runtime.clock.tick >= last_progress_tick + 100:
                    notify("running")
                    last_progress_tick = rollout.runtime.clock.tick
        observations = environment.observe_dual(policy_features_only=True)
        if (set(observations) != {0, 1} or any(item.truncated for item in observations.values())
                or observations[0].terminal != observations[1].terminal):
            raise ValueError("migration terminal views disagree or contain truncation")
        before = monotonic()
        publication = recorder.publish(terminal=observations[0].terminal, raw=environment.raw_observation,
            runtime=rollout.runtime)
        timings["publish_seconds"] += monotonic() - before
        episode, receipt = store.load(match_id)
        report = dict(schema="fair-final-native-migration-episode.v1", status="passed", match_id=match_id,
            attempt_id=attempt_id, publication=publication, receipt=receipt,
            terminal=episode["result"], rows=len(recorder.rows),
            rows_by_owner={str(owner): sum(row["owner"] == owner for row in recorder.rows) for owner in (0, 1)},
            lifecycle=dict(Counter(row["outcome"]["reason"] for row in recorder.rows)),
            timings=dict(timings), seconds=monotonic() - started,
            source_network_sha=semantic_digest(teacher.state_dict()), training_admissible=False)
        notify("passed", publication=publication)
        return report
    except Exception as error:
        if not recorder.sealed:
            recorder.quarantine(f"{type(error).__name__}: {error}")
        notify("failed", error=f"{type(error).__name__}: {error}")
        raise
