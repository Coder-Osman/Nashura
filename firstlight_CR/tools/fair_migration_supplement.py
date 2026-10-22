import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.contracts import integer
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_migration_collect import collection_protocols
from native_runner.training.fair_migration_data import (MigrationDataset, MigrationRecorder, MigrationStore,
    teacher_targets, validate_manifest, validate_teacher)
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.diagnose_fair_final_migration import load_diagnostic_student


def validate_student(student, expected_network_sha, teacher):
    require_sha(expected_network_sha)
    if (not isinstance(student, ConditionalPolicyValue)
            or any(module.training for module in student.modules())
            or any(parameter.requires_grad or parameter.dtype != torch.float32 for parameter in student.parameters())
            or semantic_digest(student.state_dict()) != expected_network_sha
            or student.encoder.catalog.catalog_id != teacher.policy.backbone.catalog.catalog_id):
        raise ValueError("unchanged frozen FP32 conditional migration student required")


def validate_supplement_round(manifest, parent_manifest, student_checkpoint_sha):
    require_sha(student_checkpoint_sha)
    validate_manifest(manifest)
    validate_manifest(parent_manifest)
    if (manifest["round"] not in (1, 2) or parent_manifest["round"] != manifest["round"] - 1
            or manifest["identity"] != parent_manifest["identity"]
            or manifest["card_ids"] != parent_manifest["card_ids"]
            or manifest["namespace_sha"] != parent_manifest["namespace_sha"]
            or set(manifest["matches"]) & set(parent_manifest["matches"])
            or {task["pair_seed_id"] for task in manifest["matches"].values()}
                & {task["pair_seed_id"] for task in parent_manifest["matches"].values()}):
        raise ValueError("disjoint consecutive supplementary migration rounds required")
    source_sha = manifest["identity"]["source_a_sha"]
    if student_checkpoint_sha == source_sha:
        raise ValueError("distinct supplementary student checkpoint required")
    pairs = {}
    for task in manifest["matches"].values():
        models = task["behavior_checkpoint_shas"]
        if sorted(models) != sorted([source_sha, student_checkpoint_sha]):
            raise ValueError("one frozen supplementary student checkpoint required")
        pairs.setdefault(task["pair_seed_id"], []).append(models.index(student_checkpoint_sha))
    if any(sorted(owners) != [0, 1] for owners in pairs.values()):
        raise ValueError("complete seat-swapped supplementary seed pairs required")


def collect_supplement_episode(environment, *, teacher, student, student_checkpoint_sha, student_network_sha,
        store, match_id, attempt_id, execution_seed, wall_seconds=1200., progress=None,
        rollout_factory=FinalRollout, monotonic=time.monotonic):
    if type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds) or wall_seconds <= 0:
        raise ValueError("positive finite migration wall limit required")
    require_sha(student_checkpoint_sha)
    validate_manifest(store.manifest)
    task = store.manifest["matches"].get(match_id)
    source_sha = store.manifest["identity"]["source_a_sha"]
    if (store.manifest["round"] not in (1, 2) or task is None or student_checkpoint_sha == source_sha
            or sorted(task["behavior_checkpoint_shas"]) != sorted([source_sha, student_checkpoint_sha])):
        raise ValueError("registered supplementary A-versus-student checkpoint pair required")
    validate_teacher(teacher, store.manifest["identity"])
    validate_student(student, student_network_sha, teacher)
    student_owner = task["behavior_checkpoint_shas"].index(student_checkpoint_sha)
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
                    raise TimeoutError("complete supplementary migration episode wall limit exceeded")
                before = monotonic()
                frozen = rollout.freeze_due_information()
                timings["freeze_seconds"] += monotonic() - before
                if frozen:
                    before = monotonic()
                    frozen_shas = {owner: info.sha256 for owner, info in frozen.items()}
                    selected = {}
                    for owner, info in sorted(frozen.items()):
                        if owner == student_owner:
                            policies, _ = student(info.frame, [info.legality])
                            action, _ = policies[0].sample(torch.Generator().manual_seed(0), greedy=True)
                        else:
                            action = teacher_targets(teacher, info)[0]
                        if not info.legality.legal(action):
                            raise ValueError("supplementary behavior action is illegal")
                        selected[owner] = action
                    if frozen_shas != {owner: info.sha256 for owner, info in frozen.items()}:
                        raise ValueError("supplementary inference changed frozen FAIR information")
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
            raise ValueError("supplementary migration terminal views disagree or contain truncation")
        validate_student(student, student_network_sha, teacher)
        before = monotonic()
        publication = recorder.publish(terminal=observations[0].terminal, raw=environment.raw_observation,
            runtime=rollout.runtime)
        timings["publish_seconds"] += monotonic() - before
        episode, receipt = store.load(match_id)
        report = dict(schema="fair-final-supplementary-migration-episode.v1", status="passed",
            match_id=match_id, attempt_id=attempt_id, round=store.manifest["round"],
            publication=publication, receipt=receipt, terminal=episode["result"], rows=len(recorder.rows),
            rows_by_owner={str(owner): sum(row["owner"] == owner for row in recorder.rows) for owner in (0, 1)},
            lifecycle=dict(Counter(row["outcome"]["reason"] for row in recorder.rows)),
            student_owner=student_owner, student_checkpoint_sha=student_checkpoint_sha,
            student_network_sha=student_network_sha, source_network_sha=semantic_digest(teacher.state_dict()),
            decoding="student-greedy-versus-A-adapter-greedy", timings=dict(timings),
            seconds=monotonic() - started, training_admissible=False)
        notify("passed", publication=publication)
        return report
    except Exception as error:
        if not recorder.sealed:
            recorder.quarantine(f"{type(error).__name__}: {error}")
        notify("failed", error=f"{type(error).__name__}: {error}")
        raise


def load_supplement_student(path, *, expected_sha, dataset, device, bundle=None):
    rng = torch.get_rng_state()
    try:
        student = load_diagnostic_student(path, expected_sha=expected_sha, dataset=dataset, device=device, bundle=bundle)
        student.requires_grad_(False)
        validate_student(student, semantic_digest(student.state_dict()), dataset.teacher)
        return student
    finally:
        torch.set_rng_state(rng)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parent-data", type=Path, required=True)
    parser.add_argument("--parent-manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--student", type=Path, required=True)
    parser.add_argument("--student-sha256", required=True)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--attempt-id", type=int, default=1)
    parser.add_argument("--engine-seed", type=int, required=True)
    parser.add_argument("--execution-seed", type=int, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--wall-seconds", type=float, default=1200)
    parser.add_argument("--engineering-acceptance", action="store_true", required=True)
    args = parser.parse_args()
    require_sha(args.match_id)
    integer(args.attempt_id, 1, "supplementary attempt")
    if args.attempt_id > 3:
        raise ValueError("supplementary migration retry limit exceeded")
    torch.set_num_threads(1)
    manifest = json.loads(args.manifest.read_text())
    parent_manifest = json.loads(args.parent_manifest.read_text())
    validate_supplement_round(manifest, parent_manifest, args.student_sha256)
    if any(manifest["identity"][key] != value for key, value in collection_protocols(args.engine_sha256).items()):
        raise ValueError("supplementary migration protocol identity differs")
    source, _, source_sha = load_checkpoint(args.source, device=args.device,
        expected_sha256=manifest["identity"]["source_a_sha"])
    source.eval().requires_grad_(False)
    parent = MigrationDataset(MigrationStore(args.parent_data, manifest=parent_manifest), teacher=source)
    student = load_supplement_student(args.student, expected_sha=args.student_sha256, dataset=parent, device=args.device)
    network_sha = semantic_digest(student.state_dict())
    store = MigrationStore(args.output, manifest=manifest)
    audit = store.root / "audit" / args.match_id / str(args.attempt_id)
    audit.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    files = [path for path in (project / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(project).parts]
    files += [Path(__file__).resolve(), project / "tools/diagnose_fair_final_migration.py",
        project / "tools/fair_migration_diagnostics.py"]
    source_manifest = {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)}
    atomic_json(audit / "private-inputs.json", dict(engine_seed=args.engine_seed, execution_seed=args.execution_seed,
        purpose="supplementary-migration-engineering-acceptance", source_sha256=source_sha,
        student_checkpoint_sha256=args.student_sha256, student_network_sha=network_sha,
        parent_dataset_sha=parent.identity["dataset_sha"], source_sha256_manifest=source_manifest))
    progress_path = store.root / "progress" / f"{args.match_id}-{args.attempt_id}.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    native = None
    try:
        native = create_search_native(args.port, transport="compact-delta")
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
            seed=args.engine_seed, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options={"render_mode": "headless", "decision_ticks": 1,
                "event_driven_decisions": False, "allow_initial_remaining_runtime_rejections": True})
        native.start_atomic_steps()
        report = collect_supplement_episode(environment, teacher=source, student=student,
            student_checkpoint_sha=args.student_sha256, student_network_sha=network_sha,
            store=store, match_id=args.match_id, attempt_id=args.attempt_id, execution_seed=args.execution_seed,
            wall_seconds=args.wall_seconds, progress=lambda value: atomic_json(progress_path, value))
        atomic_json(audit / "native-terminal.json", environment.raw_observation)
        report.update(source_sha256=source_manifest, engine_sha256=args.engine_sha256)
        atomic_json(store.root / "attempts" / args.match_id / str(args.attempt_id) / "report.json", report)
        print(json.dumps(report), flush=True)
    finally:
        if native is not None:
            native.close_transport()


if __name__ == "__main__":
    main()
