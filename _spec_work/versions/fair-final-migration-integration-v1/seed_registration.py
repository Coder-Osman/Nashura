import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "fair-final-migration-series-v1"),
    str(ROOT.parents[2] / "firstlight_CR")]

from migration_series import validate_engine_seed, validate_series
from seed_sources import verify_seed_sources
from native_runner.training.fair_final.contracts import exact_fields
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


SCHEMA = "FairFinalCollectionSeedRegistration/v2"
SCOPES = {"training", "value_training", "evaluation"}


def inventory_identities(paths):
    identities = []
    scopes = set()
    excluded = set()
    for path in paths:
        data = Path(path).read_bytes()
        inventory = json.loads(data)
        exact_fields(inventory, {"schema", "scope", "engine_seeds", "source_shas"}, "seed inventory")
        if inventory["schema"] != "FairFinalSeedExclusion/v1" or inventory["scope"] not in SCOPES:
            raise ValueError("seed inventory schema or scope differs")
        seeds = inventory["engine_seeds"]
        if not isinstance(seeds, list):
            raise ValueError("explicit inventory seed list required")
        for seed in seeds:
            validate_engine_seed(seed)
        if len(set(seeds)) != len(seeds):
            raise ValueError("duplicate inventory seed")
        sources = inventory["source_shas"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("seed inventory source identities required")
        for source in sources:
            require_sha(source)
        if len(set(sources)) != len(sources):
            raise ValueError("duplicate inventory source identity")
        identities.append(dict(file_sha256=hashlib.sha256(data).hexdigest(),
            semantic_sha=semantic_digest(inventory), scope=inventory["scope"],
            seed_count=len(seeds), source_shas=sources))
        scopes.add(inventory["scope"])
        excluded.update(seeds)
    if scopes != SCOPES:
        raise ValueError("training, value_training and evaluation exclusion inventories required")
    if len({entry["file_sha256"] for entry in identities}) != len(identities):
        raise ValueError("duplicate seed inventory artifact")
    return sorted(identities, key=lambda entry: entry["file_sha256"]), excluded


def source_inputs(inventory_paths, *, source_map_path, source_paths, expected_inventories):
    artifacts = {}
    for path in inventory_paths:
        data = Path(path).read_bytes()
        artifacts[hashlib.sha256(data).hexdigest()] = data
    if set(artifacts) != {entry["file_sha256"] for entry in expected_inventories}:
        raise ValueError("seed exclusion artifacts changed before source validation")
    return verify_seed_sources(artifacts, source_map_path=source_map_path, source_paths=source_paths)


def make_registration(plan, *, expected_plan_sha, inventory_paths, source_map_path, source_paths):
    if semantic_digest(plan) != require_sha(expected_plan_sha):
        raise ValueError("registered plan identity differs")
    report = validate_series(plan)
    inventories, excluded = inventory_identities(inventory_paths)
    sources, _, _ = source_inputs(inventory_paths, source_map_path=source_map_path,
        source_paths=source_paths, expected_inventories=inventories)
    if not excluded.issubset(set(plan["excluded_engine_seeds"])):
        raise ValueError("series plan omits externally excluded engine seeds")
    current = plan["rounds"][-1]
    return dict(schema=SCHEMA, plan_sha=expected_plan_sha,
        registered_round=current["manifest"]["round"],
        manifest_sha=semantic_digest(current["manifest"]),
        private_tasks_sha=semantic_digest(current["private_tasks"]),
        inventory_identities=inventories, source_evidence=sources,
        external_overlap_scope=sources["scope"],
        matches=report["rounds"][-1]["matches"], training_admissible=False, formal_g2_pass=False)


def validate_registration(registration, *, expected_sha, plan, expected_plan_sha,
        inventory_paths, source_map_path, source_paths, match_id):
    if semantic_digest(registration) != require_sha(expected_sha):
        raise ValueError("seed registration identity differs")
    expected = make_registration(plan, expected_plan_sha=expected_plan_sha,
        inventory_paths=inventory_paths, source_map_path=source_map_path, source_paths=source_paths)
    if semantic_digest(registration) != semantic_digest(expected):
        raise ValueError("seed registration plan, round or exclusion artifacts differ")
    if match_id not in plan["rounds"][-1]["manifest"]["matches"]:
        raise ValueError("task absent from registered collection round")
    return dict(seed_registration_sha=expected_sha,
        exclusion_inventory_shas=[entry["file_sha256"] for entry in expected["inventory_identities"]],
        seed_source_map_sha=expected["source_evidence"]["map_file_sha256"],
        seed_overlap_scope=expected["external_overlap_scope"])


def archive_registration(registration, *, expected_sha, inventory_paths, source_map_path,
        source_paths, output):
    if semantic_digest(registration) != require_sha(expected_sha):
        raise ValueError("seed registration identity differs")
    artifacts = {}
    for path in inventory_paths:
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in artifacts:
            raise ValueError("duplicate seed inventory artifact")
        artifacts[digest] = data
    expected = {entry["file_sha256"] for entry in registration["inventory_identities"]}
    if set(artifacts) != expected:
        raise ValueError("seed exclusion artifacts changed before archival")
    sources, map_bytes, source_artifacts = verify_seed_sources(artifacts,
        source_map_path=source_map_path, source_paths=source_paths)
    if semantic_digest(sources) != semantic_digest(registration["source_evidence"]):
        raise ValueError("seed source evidence changed before archival")
    output = Path(output)
    for digest, data in source_artifacts.items():
        ensure_immutable(output / "sources" / (digest + ".json"), data)
    ensure_immutable(output / "source-map.json", map_bytes)
    for digest, data in artifacts.items():
        ensure_immutable(output / (digest + ".json"), data)
    ensure_immutable(output / "registration.json", json_bytes(registration))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engineering-acceptance", action="store_true", required=True)
    parser.add_argument("--private-plan", type=Path, required=True)
    parser.add_argument("--plan-sha", required=True)
    parser.add_argument("--excluded-seed-inventories", type=Path, nargs="+", required=True)
    parser.add_argument("--seed-source-map", type=Path, required=True)
    parser.add_argument("--seed-source-artifacts", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.private_plan.read_text())
    registration = make_registration(plan, expected_plan_sha=args.plan_sha,
        inventory_paths=args.excluded_seed_inventories, source_map_path=args.seed_source_map,
        source_paths=args.seed_source_artifacts)
    ensure_immutable(args.output, json_bytes(registration))
    print(json.dumps(dict(registration_sha=semantic_digest(registration),
        registered_round=registration["registered_round"], matches=registration["matches"],
        training_admissible=False, formal_g2_pass=False)))


if __name__ == "__main__":
    main()
