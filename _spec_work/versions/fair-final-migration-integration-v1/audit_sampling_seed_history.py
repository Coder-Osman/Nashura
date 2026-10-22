import argparse
import hashlib
import json
from pathlib import Path

from audit_remote_seed_history import pinned_bytes
from migration_series import validate_engine_seed
from seed_registration import SCOPES, inventory_identities
from seed_sources import verify_seed_sources
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes


HOSTS = ("34", "35", "37", "39", "41")
CHECKPOINT = "671f2213401ae95a2464d0ad2d44bd67e00ad85e6331ca33397f06c75764a50b"
CODE_NAME = "project/native_runner/training/fair_search/evaluate_teacher.py"


def review_sampling(directory, code_path, *, status_sha, code_sha):
    directory = Path(directory)
    status_path = directory / "status-latest.json"
    records = json.loads(pinned_bytes(status_path, status_sha))
    pinned_bytes(code_path, code_sha)
    if not isinstance(records, list) or len(records) != len(HOSTS):
        raise ValueError("complete five-host sampling result required")
    seen_hosts, seen_tasks, pointers, seeds, inputs = set(), set(), [], set(), {}
    runner_sha = hashlib.sha256((directory / "run_host.py").read_bytes()).hexdigest()
    assigned_tasks = set()
    for host_index, host in enumerate(records):
        name = host["host"]
        if name not in HOSTS or name in seen_hosts:
            raise ValueError("unknown or duplicate sampling host")
        seen_hosts.add(name)
        manifest_path = directory / ("manifest-" + name + ".json")
        raw = manifest_path.read_bytes()
        inputs[manifest_path.name] = hashlib.sha256(raw).hexdigest()
        manifest = json.loads(raw)
        if (manifest["schema"] != "fair-teacher-visit-sampling-evaluation.v1"
                or manifest["host"] != name or manifest["checkpoint_sha256"] != CHECKPOINT
                or manifest["files"][CODE_NAME] != code_sha
                or manifest["files"]["run_host.py"] != runner_sha):
            raise ValueError("sampling manifest or execution code identity differs")
        validate_engine_seed(manifest["seed_base"])
        expected = set()
        for job in manifest["jobs"]:
            key = (job["group"], job["pair"], job["owner"])
            if (key in assigned_tasks or job["group"] not in ("clean", "noisy")
                    or type(job["pair"]) is not int or not 0 <= job["pair"] < 64
                    or type(job["owner"]) is not int or job["owner"] not in (0, 1)):
                raise ValueError("invalid or duplicate sampling task")
            assigned_tasks.add(key)
            expected.add(key)
        if host.get("failures") or host["status"]["stage"] != "completed":
            raise ValueError("sampling host has failures or is incomplete")
        actual = set()
        for row_index, row in enumerate(host["rows"]):
            key = (row["group"], row["pair"], row["teacher_owner"])
            validate_engine_seed(row["seed"])
            if type(row["pair"]) is not int or type(row["teacher_owner"]) is not int:
                raise ValueError("sampling result task must use integers")
            if key in seen_tasks or key not in expected or row["seed"] != manifest["seed_base"] + row["pair"]:
                raise ValueError("sampling result seed or assignment differs")
            identity, terminal = row["evaluation_identity"], row["terminal"]
            noisy = key[0] == "noisy"
            if (identity["code_sha256"] != code_sha or identity["checkpoint_sha256"] != CHECKPOINT
                    or identity["config_sha256"] != manifest["config_sha256"]
                    or identity["schema"] != "fair-teacher-evaluation.v1"
                    or identity["start_pair"] != key[1] or identity["owners"] != [key[2]]
                    or identity["seed_base"] != manifest["seed_base"] or identity["pairs"] != 1
                    or identity["particles"] != 1 or identity["simulations"] != 32
                    or identity["sampling_temperature"] != 1.
                    or identity["opponent"] != "same-policy-greedy"
                    or identity["teacher_selection"] != "sample-visits"
                    or row["teacher_selection"] != "sample-visits"
                    or identity["root_noise"] is not noisy or row["root_noise"] is not noisy):
                raise ValueError("sampling execution identity differs")
            winner = terminal["winner"]
            if (terminal["ended"] is not True or terminal["truncated"] is not False
                    or terminal["rejected"] != 0 or terminal["unconfirmed"] != 0
                    or terminal["reason"] != "native_final_result"
                    or (winner is not None and (type(winner) is not int or winner not in (0, 1)))
                    or row["winner"] != winner or row["terminal_tick"] != terminal["terminal_tick"]
                    or row["score"] != (.5 if winner is None else float(winner == key[2]))):
                raise ValueError("sampling terminal evidence differs")
            seen_tasks.add(key)
            actual.add(key)
            seeds.add(row["seed"])
            pointers.append(f"/{host_index}/rows/{row_index}/seed")
        if actual != expected:
            raise ValueError("sampling host results do not cover assigned tasks")
    if seen_tasks != {(group, pair, owner) for group in ("clean", "noisy")
            for pair in range(64) for owner in (0, 1)} or len(seeds) != 64:
        raise ValueError("complete paired sampling experiment required")
    inputs.update(status_sha256=status_sha, execution_code_sha256=code_sha, runner_sha256=runner_sha)
    return dict(inputs=inputs, result_rows=len(seen_tasks), unique_seeds=len(seeds),
        source_projection=dict(file_sha256=status_sha, engine_seed_pointers=pointers)), seeds


def extend_inventory(base, evidence_root, output, *, base_audit_sha, sampling_directory,
        code_path, status_sha, code_sha):
    base, evidence_root, output = Path(base), Path(evidence_root), Path(output)
    audit = json.loads(pinned_bytes(base / "audit.json", base_audit_sha))
    inventories = {scope: (base / (scope + ".json")).read_bytes() for scope in SCOPES}
    by_sha = {hashlib.sha256(raw).hexdigest(): raw for raw in inventories.values()}
    sources = [evidence_root / item["path"] for item in audit["reviewed_sources"]]
    identity, mapping_raw, _ = verify_seed_sources(by_sha, source_map_path=base / "source-map.json", source_paths=sources)
    if identity != audit["source_evidence"]:
        raise ValueError("base inventory source evidence differs")
    actual_identities, _ = inventory_identities([base / (scope + ".json") for scope in SCOPES])
    if actual_identities != audit["inventory_identities"]:
        raise ValueError("base inventory identities differ")
    review, seeds = review_sampling(sampling_directory, code_path, status_sha=status_sha, code_sha=code_sha)
    evaluation = json.loads(inventories["evaluation"])
    previous = set(evaluation["engine_seeds"])
    evaluation["engine_seeds"] = sorted(previous | seeds)
    evaluation["source_shas"] = sorted(set(evaluation["source_shas"]) | {status_sha})
    inventories["evaluation"] = json_bytes(evaluation)
    mapping = json.loads(mapping_raw)
    mapping["sources"].append(review["source_projection"])
    sources.append(Path(sampling_directory) / "status-latest.json")
    output.mkdir(parents=True, exist_ok=True)
    ensure_immutable(output / "source-map.json", json_bytes(mapping))
    source_evidence, _, _ = verify_seed_sources(
        {hashlib.sha256(raw).hexdigest(): raw for raw in inventories.values()},
        source_map_path=output / "source-map.json", source_paths=sources)
    repeated, repeated_seeds = review_sampling(sampling_directory, code_path, status_sha=status_sha, code_sha=code_sha)
    if repeated != review or repeated_seeds != seeds:
        raise ValueError("sampling source changed during inventory creation")
    groups = {scope: set(json.loads(raw)["engine_seeds"]) for scope, raw in inventories.items()}
    for scope, raw in inventories.items():
        ensure_immutable(output / (scope + ".json"), raw)
    report = dict(schema="FairFinalSamplingHistoricalSeedAudit/v1", status="verified_partial_inventory",
        base_audit_sha256=base_audit_sha, sampling_review=review, source_evidence=source_evidence,
        implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        unique_seeds_by_scope={scope: len(values) for scope, values in sorted(groups.items())},
        unique_excluded_seeds=len(set().union(*groups.values())), added_evaluation_seeds=len(seeds - previous),
        cross_scope_overlaps={first + ":" + second: sorted(groups[first] & groups[second])
            for first in sorted(groups) for second in sorted(groups) if first < second},
        historical_census_complete=False, historical_preregistration_proven=False,
        collection_registration_created=False, training_admissible=False, formal_g2_pass=False,
        known_gaps=["Nested policy evaluations and older experiments remain unverified.",
            "Local result and code records do not establish historical preregistration or raw remote revalidation."])
    ensure_immutable(output / "audit.json", json_bytes(report))
    return report


def main():
    parser = argparse.ArgumentParser()
    for name in ("base", "evidence-root", "output", "sampling-directory", "code-path"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("base-audit-sha", "status-sha", "code-sha"):
        parser.add_argument("--" + name, required=True)
    report = extend_inventory(**vars(parser.parse_args()))
    print(json.dumps({key: report[key] for key in ("status", "unique_seeds_by_scope",
        "unique_excluded_seeds", "added_evaluation_seeds", "formal_g2_pass")}))


if __name__ == "__main__":
    main()
