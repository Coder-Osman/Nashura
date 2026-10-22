import argparse
import hashlib
import json
from pathlib import Path

from audit_remote_seed_history import pinned_bytes
from migration_series import validate_engine_seed
from seed_registration import SCOPES, inventory_identities
from seed_sources import verify_seed_sources
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes


CAPACITY = {"34": 4, "35": 32, "37": 32, "39": 24, "41": 32}


def review_policy_screen(directory, *, status_sha, export_sha):
    directory = Path(directory)
    records = json.loads(pinned_bytes(directory / "status-latest.json", status_sha))
    exported = json.loads(pinned_bytes(directory / "export.json", export_sha))
    if not isinstance(records, list) or len(records) != len(CAPACITY):
        raise ValueError("complete historical policy screen host set required")
    seen_hosts, seen_tasks, seen_shards, pointers, seeds = set(), set(), set(), [], set()
    for host_index, host in enumerate(records):
        name = host["host"]
        if name not in CAPACITY or name in seen_hosts:
            raise ValueError("duplicate or unexpected historical policy screen host")
        seen_hosts.add(name)
        shards = host["shards"]
        status, done = host["status.json"], host["done.json"]
        if (len(shards) != CAPACITY[name] or status["stage"] != "completed"
                or done["stage"] != "completed" or status["active"] != 0
                or status["pending"] != 0 or status["exits"] != done["exits"]):
            raise ValueError("historical policy screen has unfinished shards")
        exits = {}
        for exit_record in status["exits"]:
            key = exit_record["shard"]
            if key in exits or exit_record["exit_code"] != 0:
                raise ValueError("duplicate or failed historical policy screen exit")
            exits[key] = exit_record
        host_shards = set()
        for shard_index, shard in enumerate(shards):
            ordinal = list(CAPACITY).index(name)
            offset = sum(list(CAPACITY.values())[:ordinal])
            index = offset + shard_index
            expected_name = f"shard-{index:03d}"
            begin, end = index * 200 // 124, (index + 1) * 200 // 124
            if (shard["name"] != expected_name or shard["name"] in seen_shards
                    or type(shard["start_pair"]) is not int or shard["start_pair"] != begin
                    or type(shard["pairs"]) is not int or shard["pairs"] != end - begin):
                raise ValueError("historical policy screen shard assignment differs")
            seen_shards.add(shard["name"])
            host_shards.add(shard["name"])
            identity = shard["identity.json"]
            expected_identity = dict(candidate_sha256=exported["candidate"]["policy_sha256"],
                baseline_sha256=exported["baseline"]["policy_sha256"], deck="fixed-hog",
                mode="policy-direct", transport="compact-delta", seed_base=17613000,
                start_pair=begin, pairs=end - begin)
            if (identity != expected_identity or shard["exit.json"] != exits.get(shard["name"])
                    or shard["engine_ready"] is not False):
                raise ValueError("historical policy screen identity or exit differs")
            actual = set()
            for row_index, row in enumerate(shard["matches"]):
                pair, owner = row["pair"], row["candidate_owner"]
                if type(pair) is not int or type(owner) is not int:
                    raise ValueError("historical policy screen task requires integers")
                key = pair, owner
                validate_engine_seed(row["seed"])
                winner = row["winner"]
                if (not begin <= pair < end or owner not in (0, 1) or key in seen_tasks
                        or row["seed"] != 17613000 + pair
                        or (winner is not None and (type(winner) is not int or winner not in (0, 1)))
                        or row["score"] != (.5 if winner is None else float(winner == owner))
                        or type(row["terminal_tick"]) is not int or row["terminal_tick"] <= 0):
                    raise ValueError("historical policy screen result differs")
                seen_tasks.add(key)
                actual.add(key)
                seeds.add(row["seed"])
                pointers.append(f"/{host_index}/shards/{shard_index}/matches/{row_index}/seed")
            if actual != {(pair, owner) for pair in range(begin, end) for owner in (0, 1)}:
                raise ValueError("historical policy screen result coverage differs")
        if host_shards != set(exits):
            raise ValueError("historical policy screen exit coverage differs")
    if seen_tasks != {(pair, owner) for pair in range(200) for owner in (0, 1)}:
        raise ValueError("all paired historical policy screen results required")
    return dict(status_sha256=status_sha, export_sha256=export_sha,
        result_rows=len(seen_tasks), unique_seeds=len(seeds),
        execution_code_identity_proven=False, native_terminal_reverified=False,
        source_projection=dict(file_sha256=status_sha, engine_seed_pointers=pointers)), seeds


def extend_inventory(base, output, *, base_audit_sha, source_paths, screen_directory,
        status_sha, export_sha):
    base, output = Path(base), Path(output)
    audit = json.loads(pinned_bytes(base / "audit.json", base_audit_sha))
    inventories = {scope: (base / (scope + ".json")).read_bytes() for scope in SCOPES}
    by_sha = {hashlib.sha256(raw).hexdigest(): raw for raw in inventories.values()}
    identity, mapping_raw, _ = verify_seed_sources(by_sha,
        source_map_path=base / "source-map.json", source_paths=source_paths)
    if identity != audit["source_evidence"]:
        raise ValueError("base historical seed source identity differs")
    counts = {scope: len(json.loads(raw)["engine_seeds"]) for scope, raw in inventories.items()}
    _, excluded = inventory_identities([base / (scope + ".json") for scope in SCOPES])
    if counts != audit["unique_seeds_by_scope"] or len(excluded) != audit["unique_excluded_seeds"]:
        raise ValueError("base historical seed inventory counts differ")
    review, seeds = review_policy_screen(screen_directory, status_sha=status_sha, export_sha=export_sha)
    evaluation = json.loads(inventories["evaluation"])
    previous = set(evaluation["engine_seeds"])
    evaluation["engine_seeds"] = sorted(previous | seeds)
    evaluation["source_shas"] = sorted(set(evaluation["source_shas"]) | {status_sha})
    inventories["evaluation"] = json_bytes(evaluation)
    mapping = json.loads(mapping_raw)
    mapping["sources"].append(review["source_projection"])
    output.mkdir(parents=True, exist_ok=True)
    ensure_immutable(output / "source-map.json", json_bytes(mapping))
    source_evidence, _, _ = verify_seed_sources(
        {hashlib.sha256(raw).hexdigest(): raw for raw in inventories.values()},
        source_map_path=output / "source-map.json",
        source_paths=[*source_paths, Path(screen_directory) / "status-latest.json"])
    repeated, repeated_seeds = review_policy_screen(screen_directory,
        status_sha=status_sha, export_sha=export_sha)
    if repeated != review or repeated_seeds != seeds:
        raise ValueError("historical policy screen changed during audit")
    for scope, raw in inventories.items():
        ensure_immutable(output / (scope + ".json"), raw)
    groups = {scope: set(json.loads(raw)["engine_seeds"]) for scope, raw in inventories.items()}
    identities, excluded = inventory_identities([output / (scope + ".json") for scope in SCOPES])
    report = dict(schema="FairFinalPolicyHistoricalSeedAudit/v1", status="verified_partial_inventory",
        base_audit_sha256=base_audit_sha, policy_screen_review=review,
        source_evidence=source_evidence, inventory_identities=identities,
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        unique_seeds_by_scope={scope: len(values) for scope, values in sorted(groups.items())},
        unique_excluded_seeds=len(excluded), added_evaluation_seeds=len(seeds - previous),
        cross_scope_overlaps={first + ":" + second: sorted(groups[first] & groups[second])
            for first in sorted(groups) for second in sorted(groups) if first < second},
        historical_census_complete=False, historical_preregistration_proven=False,
        collection_registration_created=False, training_admissible=False, formal_g2_pass=False,
        known_gaps=["Nested policy evaluation schedules and other legacy experiments remain unverified.",
            "Legacy policy rows lack per-match terminal evidence and an execution-code digest; seeds are excluded conservatively.",
            "Current local records do not establish historical preregistration or raw remote revalidation."])
    ensure_immutable(output / "audit.json", json_bytes(report))
    return report


def main():
    parser = argparse.ArgumentParser()
    for name in ("base", "output", "screen-directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-path", type=Path, action="append", required=True, dest="source_paths")
    for name in ("base-audit-sha", "status-sha", "export-sha"):
        parser.add_argument("--" + name, required=True)
    report = extend_inventory(**vars(parser.parse_args()))
    print(json.dumps({key: report[key] for key in ("status", "unique_seeds_by_scope",
        "unique_excluded_seeds", "added_evaluation_seeds", "formal_g2_pass")}))


if __name__ == "__main__":
    main()
