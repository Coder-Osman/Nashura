import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

import torch

from migration_series import validate_series
from round_selection import SelectionConfig, implementation_sha, load_selected_student
from series_training import SeriesMigrationDataset
from seed_registration import archive_registration, validate_registration
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.contracts import integer
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_collect import collection_protocols
from native_runner.training.fair_migration_data import MigrationStore
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native
from tools.fair_migration_supplement import (collect_supplement_episode, validate_student,
    validate_supplement_round)


def prepare_selected_task(plan, parent_roots, *, expected_plan_sha, teacher, selected_path,
        selected_sha, weights, config, match_id, bundle=None, device="cpu"):
    if semantic_digest(plan) != require_sha(expected_plan_sha):
        raise ValueError("preregistered series plan identity differs")
    validate_series(plan)
    if len(plan["rounds"]) not in (2, 3) or len(parent_roots) != len(plan["rounds"]) - 1:
        raise ValueError("complete parent history and one registered supplementary round required")
    current = plan["rounds"][-1]
    manifest = current["manifest"]
    if match_id not in manifest["matches"]:
        raise ValueError("match absent from preregistered supplementary round")
    validate_supplement_round(manifest, plan["rounds"][-2]["manifest"], selected_sha)
    parent_plan = deepcopy(plan)
    parent_plan["rounds"].pop()
    parent = SeriesMigrationDataset(parent_plan, parent_roots, teacher=teacher)
    student = load_selected_student(selected_path, expected_sha=selected_sha,
        dataset=parent, weights=weights, config=config, bundle=bundle, device=device)
    network_sha = semantic_digest(student.state_dict())
    validate_student(student, network_sha, teacher)
    private = dict(current["private_tasks"][match_id],
        purpose="selected-supplementary-migration-engineering-acceptance",
        source_sha256=manifest["identity"]["source_a_sha"],
        student_checkpoint_sha256=selected_sha, student_network_sha=network_sha,
        parent_dataset_sha=parent.identity["dataset_sha"],
        parent_series_sha=parent.report["series_sha"], preregistered_plan_sha=expected_plan_sha,
        selection_implementation_sha=implementation_sha(),
        training_admissible=False, formal_g2_pass=False)
    return student, deepcopy(manifest), private


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engineering-acceptance", action="store_true", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-plan", type=Path, required=True)
    parser.add_argument("--plan-sha", required=True)
    parser.add_argument("--seed-registration", type=Path, required=True)
    parser.add_argument("--seed-registration-sha", required=True)
    parser.add_argument("--excluded-seed-inventories", type=Path, nargs="+", required=True)
    parser.add_argument("--seed-source-map", type=Path, required=True)
    parser.add_argument("--seed-source-artifacts", type=Path, nargs="+", required=True)
    parser.add_argument("--parent-data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--selected", type=Path, required=True)
    parser.add_argument("--selected-sha", required=True)
    parser.add_argument("--loss-weights", type=Path, required=True)
    parser.add_argument("--selection-config", type=Path, required=True)
    parser.add_argument("--engine-sha", required=True)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--attempt-id", type=int, default=1)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--wall-seconds", type=float, default=1200.)
    args = parser.parse_args()
    integer(args.attempt_id, 1, "collection attempt")
    if args.attempt_id > 3:
        raise ValueError("supplementary collection attempt budget exceeded")
    torch.set_num_threads(1)
    plan = json.loads(args.private_plan.read_text())
    if semantic_digest(plan) != require_sha(args.plan_sha):
        raise ValueError("preregistered series plan identity differs")
    validate_series(plan)
    registration = json.loads(args.seed_registration.read_text())
    seed_evidence = validate_registration(registration,
        expected_sha=args.seed_registration_sha, plan=plan, expected_plan_sha=args.plan_sha,
        inventory_paths=args.excluded_seed_inventories, source_map_path=args.seed_source_map,
        source_paths=args.seed_source_artifacts, match_id=args.match_id)
    manifest = plan["rounds"][-1]["manifest"]
    if any(manifest["identity"][key] != value for key, value in collection_protocols(args.engine_sha).items()):
        raise ValueError("selected collection engine or rule protocol differs")
    teacher, _, _ = load_checkpoint(args.source, device=args.device,
        expected_sha256=manifest["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    student, manifest, private = prepare_selected_task(plan, args.parent_data_roots,
        expected_plan_sha=args.plan_sha, teacher=teacher, selected_path=args.selected,
        selected_sha=args.selected_sha, weights=MigrationLossWeights(**json.loads(args.loss_weights.read_text())),
        config=SelectionConfig(**json.loads(args.selection_config.read_text())),
        match_id=args.match_id, device=args.device)
    store = MigrationStore(args.output, manifest=manifest)
    audit = store.root / "audit" / args.match_id / str(args.attempt_id)
    audit.mkdir(parents=True, exist_ok=False)
    archive_registration(registration, expected_sha=args.seed_registration_sha,
        inventory_paths=args.excluded_seed_inventories, source_map_path=args.seed_source_map,
        source_paths=args.seed_source_artifacts, output=audit / "seed-registration")
    project = Path(sys.modules["native_runner"].__file__).resolve().parent.parent
    dependencies = [path for path in (project / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(project).parts]
    dependencies.append(project / "tools/fair_migration_supplement.py")
    source_manifest = {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(dependencies)}
    private.update(source_sha256_manifest=source_manifest,
        integration_entrypoint_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        seed_registration_implementation_sha=hashlib.sha256((ROOT / "seed_registration.py").read_bytes()).hexdigest(),
        seed_sources_implementation_sha=hashlib.sha256((ROOT / "seed_sources.py").read_bytes()).hexdigest(),
        **seed_evidence)
    atomic_json(audit / "private-inputs.json", private)
    progress = store.root / "progress" / f"{args.match_id}-{args.attempt_id}.json"
    progress.parent.mkdir(parents=True, exist_ok=True)
    native = None
    try:
        native = create_search_native(args.port, transport="compact-delta")
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK),
            seed=private["engine_seed"], end_tick=NATIVE_FINALIZATION_DEADLINE_TICK,
            **tournament_level_kwargs()), options=dict(render_mode="headless", decision_ticks=1,
                event_driven_decisions=False, allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        report = collect_supplement_episode(environment, teacher=teacher, student=student,
            student_checkpoint_sha=args.selected_sha, student_network_sha=private["student_network_sha"],
            store=store, match_id=args.match_id, attempt_id=args.attempt_id,
            execution_seed=private["execution_seed"], wall_seconds=args.wall_seconds,
            progress=lambda value: atomic_json(progress, value))
        atomic_json(audit / "native-terminal.json", environment.raw_observation)
        report.update(source_sha256=source_manifest, engine_sha256=args.engine_sha,
            integration_entrypoint_sha=private["integration_entrypoint_sha"],
            preregistered_plan_sha=args.plan_sha, parent_series_sha=private["parent_series_sha"])
        atomic_json(store.root / "attempts" / args.match_id / str(args.attempt_id) / "report.json", report)
        print(json.dumps(report), flush=True)
    finally:
        if native is not None:
            native.close_transport()


if __name__ == "__main__":
    main()
