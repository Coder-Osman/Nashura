import argparse
import json
from pathlib import Path
import sys
import time


def validate_jobs(jobs):
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("resident collection manifest requires jobs")
    destinations = set()
    allowed = {"port", "destination", "seed", "device", "checkpoint", "expected_sha256",
               "opponent_kind", "max_seconds", "config_path", "plan_path"}
    for job in jobs:
        if (not isinstance(job, dict) or set(job) - allowed or
                not {"port", "destination", "seed", "checkpoint", "expected_sha256"} <= set(job)):
            raise ValueError("resident collection job fields differ")
        if type(job["port"]) is not int or not 1 <= job["port"] <= 65535 or type(job["seed"]) is not int:
            raise ValueError("resident collection job port or seed is invalid")
        identity = job["expected_sha256"]
        if (not isinstance(identity, str) or len(identity) != 64 or
                any(character not in "0123456789abcdef" for character in identity)):
            raise ValueError("resident collection job must pin its parent checkpoint")
        destination = Path(job["destination"]).resolve()
        if destination in destinations or destination.exists():
            raise ValueError("resident collection destination is occupied or duplicated")
        destinations.add(destination)
    return jobs


def run_jobs(jobs, *, collect, pool):
    jobs = validate_jobs(jobs)
    results = []
    for index, job in enumerate(jobs):
        started = time.perf_counter()
        result = collect(**job, policy_pool=pool)
        if result.get("passed") is not True or result.get("checkpoint_sha256") != job["expected_sha256"]:
            raise ValueError("resident collection result failed or changed its model identity")
        results.append(dict(index=index, seconds=time.perf_counter() - started,
                            destination=job["destination"], checkpoint_sha256=result["checkpoint_sha256"],
                            policy_pool=dict(pool.profile)))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-policies", type=int, default=2)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    root = args.project.resolve() / "research/puct"
    sys.path[:0] = [str(root), str(args.project.resolve()), str(root / "runtime")]
    from runtime_contract import install

    install()
    from collect_policy_episode import run
    from resident_policy_pool import ResidentPolicyPool

    pool = ResidentPolicyPool(max_policies=args.max_policies)
    try:
        jobs = json.loads(args.manifest.read_text())
        records = run_jobs(jobs, collect=run, pool=pool)
        result = dict(schema="resident-on-policy-collection.v1", passed=True, records=records,
                      policy_pool=dict(pool.profile),
                      rng_contract="actor-seed-independent-of-checkpoint-loading.v1")
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        temporary.replace(args.output)
        print(json.dumps(result), flush=True)
    finally:
        pool.clear()


if __name__ == "__main__":
    main()
