import argparse
import hashlib
import json
from pathlib import Path
import sys

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.adapter import ADAPTER_ID
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_migration_collect import collect_migration_episode, collection_protocols
from native_runner.training.fair_migration_data import MigrationStore, validate_teacher
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--attempt-id", type=int, default=1)
    parser.add_argument("--engine-seed", type=int, required=True)
    parser.add_argument("--execution-seed", type=int, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--wall-seconds", type=float, default=1200)
    parser.add_argument("--engineering-acceptance", action="store_true", required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    source_manifest = json.loads(args.source_manifest.read_text())
    project = Path(__file__).resolve().parents[1]
    required = {str(path.relative_to(project)) for path in (project / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(project).parts}
    required.add("tools/collect_fair_final_migration.py")
    if set(source_manifest) != required:
        raise ValueError("migration source manifest omits collector dependencies")
    for relative, expected in source_manifest.items():
        path = (project / relative).resolve()
        if not path.is_relative_to(project) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("deployed migration collector source identity differs")
    store = MigrationStore(args.output, manifest=json.loads(args.manifest.read_text()))
    if any(store.manifest["identity"][key] != value
            for key, value in collection_protocols(args.engine_sha256).items()):
        raise ValueError("migration collector protocol identity differs")
    if store.manifest["identity"]["migration_adapter_sha"] != semantic_digest(ADAPTER_ID):
        raise ValueError("migration adapter identity differs")
    source, _, source_sha = load_checkpoint(args.source, device=args.device,
        expected_sha256=store.manifest["identity"]["source_a_sha"])
    source.eval().requires_grad_(False)
    validate_teacher(source, store.manifest["identity"])
    audit = args.output / "audit" / args.match_id / str(args.attempt_id)
    audit.mkdir(parents=True, exist_ok=False)
    atomic_json(audit / "private-inputs.json", dict(engine_seed=args.engine_seed, execution_seed=args.execution_seed,
        purpose="native-migration-engineering-acceptance", source_sha256=source_sha,
        source_manifest_sha256=hashlib.sha256(args.source_manifest.read_bytes()).hexdigest()))
    progress_path = args.output / "progress" / f"{args.match_id}-{args.attempt_id}.json"
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
        report = collect_migration_episode(environment, teacher=source, store=store, match_id=args.match_id,
            attempt_id=args.attempt_id, execution_seed=args.execution_seed, wall_seconds=args.wall_seconds,
            progress=lambda value: atomic_json(progress_path, value))
        atomic_json(audit / "native-terminal.json", environment.raw_observation)
        report.update(source_sha256=source_manifest, engine_sha256=args.engine_sha256)
        atomic_json(store.root / "attempts" / args.match_id / str(args.attempt_id) / "report.json", report)
        print(json.dumps(report), flush=True)
    finally:
        if native is not None:
            native.close_transport()


if __name__ == "__main__":
    main()
