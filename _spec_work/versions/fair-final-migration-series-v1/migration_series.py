import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys


SOURCE = Path(__file__).resolve().parents[3] / "firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.contracts import exact_fields, integer
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes
from native_runner.training.fair_migration_data import MigrationStore, validate_manifest
from native_runner.training.fair_search.contracts import semantic_digest
from tools.fair_migration_supplement import validate_supplement_round


SCHEMA = "FairFinalMigrationSeries/v1"
SPLITS = ("train", "validation", "test")


def validate_engine_seed(seed):
    integer(seed, 0, "canonical native engine seed")
    if seed >= 1 << 32:
        raise ValueError("canonical unsigned 32-bit native engine seed required")


def validate_series(plan, *, require_final_round=False):
    exact_fields(plan, {"schema", "rounds", "excluded_engine_seeds"}, "migration series")
    rounds = plan["rounds"]
    if (plan["schema"] != SCHEMA or not isinstance(rounds, list) or not 1 <= len(rounds) <= 3
            or (require_final_round and len(rounds) != 3)):
        raise ValueError("consecutive migration series starting at round zero required")
    excluded = plan["excluded_engine_seeds"]
    if not isinstance(excluded, list):
        raise ValueError("explicit excluded engine seed list required")
    for seed in excluded:
        validate_engine_seed(seed)
    if len(set(excluded)) != len(excluded):
        raise ValueError("duplicate excluded engine seeds")
    excluded = set(excluded)
    matches, pairs, engine_seeds = set(), set(), {}
    first = None
    summaries = []
    for number, entry in enumerate(rounds):
        exact_fields(entry, {"manifest", "private_tasks"}, "migration series round")
        manifest, private = entry["manifest"], entry["private_tasks"]
        manifest_sha = validate_manifest(manifest)
        if manifest["round"] != number:
            raise ValueError("migration series omits or reorders a round")
        if first is None:
            first = manifest
        if any(manifest[key] != first[key] for key in ("identity", "namespace_sha", "card_ids")):
            raise ValueError("migration series protocol, source, deck or namespace differs")
        tasks = manifest["matches"]
        current_pairs = {task["pair_seed_id"] for task in tasks.values()}
        if matches.intersection(tasks) or pairs.intersection(current_pairs):
            raise ValueError("migration task or pair overlaps any earlier round")
        if number:
            source = manifest["identity"]["source_a_sha"]
            students = {model for task in tasks.values() for model in task["behavior_checkpoint_shas"]
                if model != source}
            if len(students) != 1:
                raise ValueError("one frozen student per supplementary round required")
            validate_supplement_round(manifest, rounds[number - 1]["manifest"], next(iter(students)))
        if not isinstance(private, dict) or set(private) != set(tasks):
            raise ValueError("exact private seed registration for every migration task required")
        pair_seeds = {}
        for match_id, task in tasks.items():
            seeds = private[match_id]
            exact_fields(seeds, {"engine_seed", "execution_seed"}, "private migration seeds")
            for key, seed in seeds.items():
                integer(seed, 0, key)
            seed = seeds["engine_seed"]
            validate_engine_seed(seed)
            pair_key = (number, task["pair_seed_id"], task["split"])
            if seed in excluded or engine_seeds.setdefault(seed, pair_key) != pair_key:
                raise ValueError("actual engine seed overlaps an independent pair, round or excluded set")
            if pair_seeds.setdefault(task["pair_seed_id"], seed) != seed:
                raise ValueError("seat-swapped migration pair uses different engine seeds")
        matches.update(tasks)
        pairs.update(current_pairs)
        counts = Counter(task["split"] for task in tasks.values())
        summaries.append(dict(round=number, manifest_sha=manifest_sha, matches=len(tasks),
            pairs=len(current_pairs), splits={split: counts[split] for split in SPLITS}))
    return dict(schema=SCHEMA, plan_sha=semantic_digest(plan), rounds=summaries,
        matches=len(matches), pairs=len(pairs), unique_engine_seeds=len(engine_seeds),
        excluded_engine_seeds=len(excluded), external_overlap_scope="provided_seed_set_only",
        final_round_present=len(rounds) == 3, training_admissible=False, formal_g2_pass=False)


class ReadOnlyMigrationStore(MigrationStore):
    def __init__(self, root, *, manifest):
        self.root = Path(root)
        self.manifest = deepcopy(manifest)
        self.dataset_sha = validate_manifest(self.manifest)
        if json.loads((self.root / "dataset.json").read_text()) != self.manifest:
            raise ValueError("stored migration manifest differs from registered round")

    def publish(self, episode):
        raise TypeError("migration series audit stores are read only")


def audit_completed_series(plan, data_roots, *, require_final_round=False):
    report = validate_series(plan, require_final_round=require_final_round)
    if len(data_roots) != len(plan["rounds"]):
        raise ValueError("one complete data root per registered migration round required")
    receipts, audits, rows = {}, {}, set()
    completed = []
    for entry, root in zip(plan["rounds"], data_roots):
        manifest = entry["manifest"]
        store = ReadOnlyMigrationStore(root, manifest=manifest)
        expected = {match_id + ".json" for match_id in manifest["matches"]}
        if {path.name for path in (store.root / "completed").iterdir()} != expected:
            raise ValueError("complete exact migration receipt set required")
        counts = Counter()
        for match_id, task in sorted(manifest["matches"].items()):
            episode, receipt = store.load(match_id)
            private_path = store.root / "audit" / match_id / str(receipt["attempt_id"]) / "private-inputs.json"
            private = json.loads(private_path.read_text())
            if (private.get("source_sha256") != manifest["identity"]["source_a_sha"]
                    or any(type(private.get(key)) is not int or private[key] != value
                        for key, value in entry["private_tasks"][match_id].items())):
                raise ValueError("successful attempt private seeds or source differ from registration")
            if manifest["round"]:
                source = manifest["identity"]["source_a_sha"]
                student = next(model for model in task["behavior_checkpoint_shas"] if model != source)
                if private.get("student_checkpoint_sha256") != student:
                    raise ValueError("successful attempt student differs from registered behavior")
            receipts[match_id] = semantic_digest(receipt)
            audits[match_id] = hashlib.sha256(private_path.read_bytes()).hexdigest()
            for row in episode["rows"]:
                if row["row_id"] in rows:
                    raise ValueError("duplicate migration row across the series")
                rows.add(row["row_id"])
            counts[task["split"]] += len(episode["rows"])
        completed.append(dict(round=manifest["round"], matches=len(manifest["matches"]),
            rows=sum(counts.values()), split_rows={split: counts[split] for split in SPLITS}))
    identity = dict(plan_sha=report["plan_sha"], receipt_shas=receipts, private_audit_shas=audits)
    return dict(report, status="engineering_series_audit_passed", series_sha=semantic_digest(identity),
        identity=identity, completed=completed, rows=len(rows),
        scope="manifest_seed_receipt_episode_consistency_only",
        native_provenance_reverified=False, teacher_targets_recomputed=False,
        training_merge_implemented=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-plan", type=Path, required=True)
    parser.add_argument("--data-roots", nargs="+", type=Path)
    parser.add_argument("--require-final-round", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.private_plan.read_text())
    if args.data_roots:
        report = audit_completed_series(plan, args.data_roots, require_final_round=args.require_final_round)
    else:
        report = dict(validate_series(plan, require_final_round=args.require_final_round),
            status="engineering_series_preflight_passed")
    report["validator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    dependencies = [path for path in (SOURCE / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(SOURCE).parts]
    dependencies += [SOURCE / "tools" / name for name in ("fair_migration_supplement.py",
        "diagnose_fair_final_migration.py", "fair_migration_diagnostics.py")]
    report["dependency_shas"] = {str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(dependencies)}
    ensure_immutable(args.output, json_bytes(report))
    print(json.dumps({key: value for key, value in report.items() if key not in ("identity", "dependency_shas")}))


if __name__ == "__main__":
    main()
