import argparse
import hashlib
import json
import math
from pathlib import Path

from audit_remote_seed_history import pinned_bytes
from collect_nested_seed_history import SLOTS, requested_files
from migration_series import validate_engine_seed
from seed_registration import SCOPES, inventory_identities
from seed_sources import verify_seed_sources
from native_runner.training.fair_final.contracts import exact_fields
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes
from native_runner.training.fair_search.contracts import require_sha


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid historical " + label)


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid historical " + label)


def review_stage(manifest, results, done, *, stage, pairs, code_sha):
    exact_fields(manifest, {"pairs", "seed_base", "jobs", "candidate.pt_sha256",
        "baseline.pt_sha256"}, "historical evaluation manifest")
    integer(manifest["pairs"], pairs, pairs, "pair count")
    validate_engine_seed(manifest["seed_base"])
    validate_engine_seed(manifest["seed_base"] + pairs - 1)
    for field in ("candidate.pt_sha256", "baseline.pt_sha256"):
        require_sha(manifest[field])
    if not isinstance(manifest["jobs"], list):
        raise ValueError("historical job list required")
    expected, assigned = {}, set()
    for job in manifest["jobs"]:
        exact_fields(job, {"id", "host", "pair", "owner"}, "historical evaluation job")
        integer(job["pair"], 0, pairs - 1, "job pair")
        integer(job["owner"], 0, 1, "job owner")
        key = job["pair"], job["owner"]
        if (job["host"] not in ("34", "35", "37", "39", "41")
                or job["id"] != f"pair-{key[0]:04d}-owner-{key[1]}"
                or job["id"] in expected or key in assigned):
            raise ValueError("historical evaluation assignment differs")
        expected[job["id"]] = key
        assigned.add(key)
    complete = {(pair, owner) for pair in range(pairs) for owner in (0, 1)}
    if assigned != complete:
        raise ValueError("complete paired historical schedule required")
    exact_fields(results, {"matches", "report"}, "historical evaluation results")
    if not isinstance(results["matches"], list):
        raise ValueError("historical result list required")
    seen, seeds, pointers, scores, ambiguous = set(), set(), [], [], []
    for index, row in enumerate(results["matches"]):
        integer(row["pair"], 0, pairs - 1, "result pair")
        integer(row["candidate_owner"], 0, 1, "result owner")
        integer(row["attempt"], 0, 2, "attempt")
        key = row["pair"], row["candidate_owner"]
        validate_engine_seed(row["seed"])
        if (expected.get(row["job_id"]) != key or key in seen
                or row["seed"] != manifest["seed_base"] + key[0]
                or row["candidate_sha256"] != manifest["candidate.pt_sha256"]
                or row["baseline_sha256"] != manifest["baseline.pt_sha256"]):
            raise ValueError("historical result task, model or seed differs")
        identity = row["evaluation_identity"]
        expected_identity = dict(candidate_sha256=manifest["candidate.pt_sha256"],
            baseline_sha256=manifest["baseline.pt_sha256"], code_sha256=code_sha,
            deck="fixed-hog", mode="policy-direct", owners=[key[1]], pairs=1,
            seed_base=manifest["seed_base"], start_pair=key[0], transport="compact-delta")
        if identity != expected_identity:
            raise ValueError("historical result execution identity differs")
        for field in ("pairs", "seed_base", "start_pair"):
            integer(identity[field], 0, (1 << 32) - 1, "identity " + field)
        if any(type(owner) is not int for owner in identity["owners"]):
            raise ValueError("historical identity owners require integers")
        terminal = row["terminal"]
        winner = terminal["winner"]
        if winner is not None:
            integer(winner, 0, 1, "terminal winner")
        if row["winner"] is not None:
            integer(row["winner"], 0, 1, "result winner")
        integer(terminal["terminal_tick"], 1, (1 << 32) - 1, "terminal tick")
        integer(row["terminal_tick"], 1, (1 << 32) - 1, "result tick")
        integer(terminal["rejected"], 0, 0, "rejections")
        integer(terminal["unconfirmed"], 0, 0, "unconfirmed actions")
        number(row["score"], "score")
        number(row["wall_seconds"], "duration")
        expected_reason = "native_final_result" if winner is not None else "draw_or_unexposed_tiebreak"
        if (terminal["ended"] is not True or terminal["truncated"] is not False
                or terminal["reason"] != expected_reason
                or row["winner"] != winner or row["terminal_tick"] != terminal["terminal_tick"]
                or row["score"] != (.5 if winner is None else float(winner == key[1]))
                or row["wall_seconds"] <= 0):
            raise ValueError("historical terminal result differs")
        seen.add(key)
        seeds.add(row["seed"])
        pointers.append(f"/matches/{index}/seed")
        scores.append(row["score"])
        if winner is None:
            ambiguous.append(row["job_id"])
    if seen != complete:
        raise ValueError("complete paired historical results required")
    report = results["report"]
    if report != done or report["stage"] != stage or report["promoted"] is not False:
        raise ValueError("historical completion record differs")
    expected_counts = dict(games=pairs * 2, required=pairs * 2, complete_pairs=pairs,
        wins=sum(score == 1 for score in scores), draws=sum(score == .5 for score in scores))
    for field, count in expected_counts.items():
        integer(report[field], count, count, "report " + field)
    paired = report["paired"]
    integer(paired["games"], pairs * 2, pairs * 2, "paired report games")
    for field in ("score", "lower95", "upper95"):
        number(paired[field], "paired " + field)
    if (paired["score"] != sum(scores) / len(scores)
            or not 0 <= paired["lower95"] <= paired["score"] <= paired["upper95"] <= 1):
        raise ValueError("historical score summary differs")
    return dict(result_rows=len(seen), unique_seeds=len(seeds),
        engine_seed_pointers=pointers, execution_code_record_matches=True,
        ambiguous_outcome_jobs=ambiguous, wdl_training_admissible=False,
        native_terminal_reverified=False, confidence_interval_recomputed=False), seeds


def review_readback(directory, *, receipt_sha):
    directory = Path(directory)
    receipt = json.loads(pinned_bytes(directory / "receipt.json", receipt_sha))
    if (receipt["schema"] != "FairFinalNestedSeedReadback/v1"
            or receipt["host"] != "172.16.48.35" or receipt["read_only"] is not True
            or receipt["remote_before_after_equal"] is not True
            or any(receipt[field] is not False for field in ("historical_preregistration_proven",
                "native_terminal_reverified", "training_admissible", "formal_g2_pass"))):
        raise ValueError("historical readback receipt scope differs")
    requested = requested_files()
    files, identities = {}, {}
    for record in receipt["files"]:
        name = record["local"]
        if name not in requested or name in files or record["remote"] != requested[name]:
            raise ValueError("historical readback file set differs")
        raw = pinned_bytes(directory / name, record["sha256"])
        integer(record["bytes"], len(raw), len(raw), "source bytes")
        files[name] = raw
        identities[name] = record["sha256"]
    if set(files) != set(requested):
        raise ValueError("complete historical readback required")
    reviews, all_seeds = [], set()
    for slot, _, stage, pairs in SLOTS:
        review, seeds = review_stage(*(json.loads(files[slot + "-" + suffix + ".json"])
            for suffix in ("manifest", "results", "done")), stage=stage, pairs=pairs,
            code_sha=identities[slot + "-evaluate_policy.py"])
        if all_seeds & seeds:
            raise ValueError("independent historical evaluation stages reuse seeds")
        all_seeds.update(seeds)
        pointers = review.pop("engine_seed_pointers")
        review.update(slot=slot, source_projection=dict(file_sha256=identities[slot + "-results.json"],
            engine_seed_pointers=pointers))
        reviews.append(review)
    return dict(receipt_sha256=receipt_sha, files=identities, stages=reviews), all_seeds


def extend_inventory(base, output, *, base_audit_sha, source_paths, readback, receipt_sha):
    base, output, readback = Path(base), Path(output), Path(readback)
    audit = json.loads(pinned_bytes(base / "audit.json", base_audit_sha))
    inventories = {scope: (base / (scope + ".json")).read_bytes() for scope in SCOPES}
    identity, mapping_raw, _ = verify_seed_sources(
        {hashlib.sha256(raw).hexdigest(): raw for raw in inventories.values()},
        source_map_path=base / "source-map.json", source_paths=source_paths)
    identities, excluded = inventory_identities([base / (scope + ".json") for scope in SCOPES])
    counts = {scope: len(json.loads(raw)["engine_seeds"]) for scope, raw in inventories.items()}
    if (identity != audit["source_evidence"] or identities != audit["inventory_identities"]
            or counts != audit["unique_seeds_by_scope"] or len(excluded) != audit["unique_excluded_seeds"]):
        raise ValueError("base historical inventory identity differs")
    review, seeds = review_readback(readback, receipt_sha=receipt_sha)
    evaluation = json.loads(inventories["evaluation"])
    previous = set(evaluation["engine_seeds"])
    evaluation["engine_seeds"] = sorted(previous | seeds)
    projections = [stage["source_projection"] for stage in review["stages"]]
    evaluation["source_shas"] = sorted(set(evaluation["source_shas"]) |
        {projection["file_sha256"] for projection in projections})
    inventories["evaluation"] = json_bytes(evaluation)
    mapping = json.loads(mapping_raw)
    mapping["sources"].extend(projections)
    output.mkdir(parents=True, exist_ok=True)
    ensure_immutable(output / "source-map.json", json_bytes(mapping))
    source_evidence, _, _ = verify_seed_sources(
        {hashlib.sha256(raw).hexdigest(): raw for raw in inventories.values()},
        source_map_path=output / "source-map.json", source_paths=[*source_paths,
            *(readback / (slot + "-results.json") for slot, _, _, _ in SLOTS)])
    repeated, repeated_seeds = review_readback(readback, receipt_sha=receipt_sha)
    if repeated != review or repeated_seeds != seeds:
        raise ValueError("historical readback changed during inventory creation")
    for scope, raw in inventories.items():
        ensure_immutable(output / (scope + ".json"), raw)
    groups = {scope: set(json.loads(raw)["engine_seeds"]) for scope, raw in inventories.items()}
    identities, excluded = inventory_identities([output / (scope + ".json") for scope in SCOPES])
    report = dict(schema="FairFinalNestedHistoricalSeedAudit/v1", status="verified_partial_inventory",
        base_audit_sha256=base_audit_sha, nested_review=review, source_evidence=source_evidence,
        inventory_identities=identities, unique_excluded_seeds=len(excluded),
        unique_seeds_by_scope={scope: len(values) for scope, values in sorted(groups.items())},
        added_evaluation_seeds=len(seeds - previous),
        cross_scope_overlaps={first + ":" + second: sorted(groups[first] & groups[second])
            for first in sorted(groups) for second in sorted(groups) if first < second},
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        historical_census_complete=False, historical_preregistration_proven=False,
        collection_registration_created=False, training_admissible=False, formal_g2_pass=False,
        known_gaps=["Other legacy experiments and unexecuted nested schedules remain unverified.",
            "Matching stored execution-code records do not reverify historical processes or raw native terminals.",
            "Legacy draw_or_unexposed_tiebreak rows are excluded as used seeds, not accepted as true draw labels.",
            "Readback time does not establish historical preregistration; no new-rules strength result is created."])
    ensure_immutable(output / "audit.json", json_bytes(report))
    return report


def main():
    parser = argparse.ArgumentParser()
    for name in ("base", "output", "readback"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-path", type=Path, action="append", required=True, dest="source_paths")
    for name in ("base-audit-sha", "receipt-sha"):
        parser.add_argument("--" + name, required=True)
    report = extend_inventory(**vars(parser.parse_args()))
    print(json.dumps({key: report[key] for key in ("status", "unique_seeds_by_scope",
        "unique_excluded_seeds", "added_evaluation_seeds", "formal_g2_pass")}))


if __name__ == "__main__":
    main()
