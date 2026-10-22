import argparse
import hashlib
import json
from pathlib import Path

from seed_registration import SCOPES, inventory_identities
from seed_sources import verify_seed_sources
from migration_series import validate_engine_seed
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes


REVIEWED_SOURCES = (
    ("training", "clean-student-loop-20261013/manifest.json", "fair-clean-student-loop.v1", "match_seed"),
    ("training", "continuous-training-20261014/round-000001-manifest.json", "fair-clean-student-loop.v1", "match_seed"),
    ("training", "new-value-teacher-20261012/manifest.json", "fair-new-value-teacher.v1", "match_seed"),
    ("training", "teacher-continuation-20261013/manifest.json", "fair-frozen-teacher-collection.v1", "match_seed"),
    ("value_training", "value-direct-20261012/manifest.json", "fair-value-run.v1", "seed"),
    ("evaluation", "teacher-r0-vs-m0-20261014/manifest.json", "fair-dual-teacher-evaluation.v1", "seed"),
)

KNOWN_GAPS = (
    "Only six reviewed local manifests are included; project-wide historical coverage is incomplete.",
    "Nested evaluation schedules without explicit match seeds require execution-code and result verification.",
    "Teacher sampling evaluation manifests have pair IDs and seed bases, not explicit match seeds.",
    "Remote bundle rounds 2/3 and legacy experiments require separate source review.",
    "Planned jobs are excluded conservatively; completion and historical preregistration are not established.",
    "This audit creates no collection registration and does not authorize formal G2 collection.",
)


def project_jobs(document, *, expected_schema, seed_key):
    if not isinstance(document, dict) or document.get("schema") != expected_schema:
        raise ValueError("reviewed historical manifest schema differs")
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("explicit nonempty historical jobs required")
    seeds, pointers = [], []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict) or seed_key not in job:
            raise ValueError("historical job lacks explicit engine seed")
        validate_engine_seed(job[seed_key])
        seeds.append(job[seed_key])
        pointers.append("/jobs/" + str(index) + "/" + seed_key)
    return seeds, pointers


def audit_history(evidence_root, output, *, sources=REVIEWED_SOURCES, known_gaps=KNOWN_GAPS):
    evidence_root, output = Path(evidence_root), Path(output)
    by_scope = {scope: dict(seeds=set(), sources=[]) for scope in SCOPES}
    source_paths, projections, reviewed = [], [], []
    for scope, relative, schema, seed_key in sources:
        if scope not in SCOPES or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("reviewed scope and local evidence path required")
        source = evidence_root / relative
        raw = source.read_bytes()
        document = json.loads(raw)
        seeds, pointers = project_jobs(document, expected_schema=schema, seed_key=seed_key)
        digest = hashlib.sha256(raw).hexdigest()
        by_scope[scope]["seeds"].update(seeds)
        by_scope[scope]["sources"].append(digest)
        source_paths.append(source)
        projections.append(dict(file_sha256=digest, engine_seed_pointers=pointers))
        reviewed.append(dict(path=relative, file_sha256=digest, bytes=len(raw), scope=scope,
            schema=schema, seed_key=seed_key, job_count=len(seeds), unique_seed_count=len(set(seeds)),
            nested_evaluations_excluded="evaluations" in document))
    mapping = json_bytes(dict(schema="FairFinalSeedSources/v1", sources=projections))
    artifacts = {}
    inventory_bytes = {}
    for scope, group in sorted(by_scope.items()):
        inventory = json_bytes(dict(schema="FairFinalSeedExclusion/v1", scope=scope,
            engine_seeds=sorted(group["seeds"]), source_shas=sorted(group["sources"])))
        artifacts[hashlib.sha256(inventory).hexdigest()] = inventory
        inventory_bytes[scope] = inventory
    output.mkdir(parents=True, exist_ok=True)
    map_path = output / "source-map.json"
    ensure_immutable(map_path, mapping)
    identity, _, _ = verify_seed_sources(artifacts, source_map_path=map_path, source_paths=source_paths)
    inventory_paths = []
    for scope, raw in inventory_bytes.items():
        path = output / (scope + ".json")
        ensure_immutable(path, raw)
        inventory_paths.append(path)
    inventories, excluded = inventory_identities(inventory_paths)
    overlap = {}
    for first in sorted(SCOPES):
        for second in sorted(SCOPES):
            if first < second:
                overlap[first + ":" + second] = sorted(by_scope[first]["seeds"] & by_scope[second]["seeds"])
    implementation = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("audit_local_seed_history.py", "seed_sources.py", "seed_registration.py")}
    report = dict(schema="FairFinalLocalHistoricalSeedAudit/v1", status="verified_partial_inventory",
        source_evidence=identity, inventory_identities=inventories, reviewed_sources=reviewed,
        unique_seeds_by_scope={scope: len(group["seeds"]) for scope, group in sorted(by_scope.items())},
        unique_excluded_seeds=len(excluded), cross_scope_overlaps=overlap,
        implementation_sha256=implementation, known_gaps=list(known_gaps),
        historical_census_complete=False, historical_preregistration_proven=False,
        collection_registration_created=False, training_admissible=False, formal_g2_pass=False)
    ensure_immutable(output / "audit.json", json_bytes(report))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_history(args.evidence_root, args.output)
    print(json.dumps(dict(status=report["status"], unique_seeds_by_scope=report["unique_seeds_by_scope"],
        unique_excluded_seeds=report["unique_excluded_seeds"], training_admissible=False, formal_g2_pass=False)))


if __name__ == "__main__":
    main()
