from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from verify_prng_evidence import validate_prng_evidence


COMMON_CASES = ("missing_shard", "metadata_only", "unchanged_digest", "wrong_algorithm",
    "wrong_engine", "wrong_process", "changed_policy", "missing_seat", "missing_stale_check",
    "restore_state", "restore_digest", "snapshot_leak", "changed_source", "changed_artifact",
    "unclean_engine", "training_claim")
SEARCH_CASES = ("fake_budget", "unfinished_backup", "short_horizon", "changed_target",
    "changed_prng_during_search", "missing_attempt", "changed_transition", "actual_state_copy",
    "hidden_history", "changed_selected")


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    phase = sys.argv[1] if len(sys.argv) > 1 else "full"
    baseline = validate_prng_evidence(root, workspace, phase)
    cases = COMMON_CASES + (SEARCH_CASES if phase == "full" else ())
    results = []
    for name in cases:
        with tempfile.TemporaryDirectory(prefix="fair-prng-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to((root / relative).resolve())
            for filename in ("run_prng_validation.py", "prng_validation_worker.py"):
                (temporary / filename).symlink_to(root / filename)

            def replace_artifact(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/" + suffix))
                target = temporary / relative
                if target.is_symlink():
                    target.unlink()
                target.write_text(json.dumps(payload, sort_keys=True))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            shard = report["shards"][0]
            native = shard["report"]
            boundary = native["boundary"]
            mutation = boundary["mutation"]
            entry = native["records"][0] if native["records"] else None
            if name == "missing_shard":
                report["shards"].pop()
            elif name == "metadata_only":
                mutation["after"] = mutation["before"]
            elif name == "unchanged_digest":
                mutation["after_digest"] = mutation["before_digest"]
            elif name == "wrong_algorithm":
                mutation["algorithm"] = "metadata-seed-only"
            elif name == "wrong_engine":
                mutation["probe_sha256"] = "0" * 64
            elif name == "wrong_process":
                mutation["child_pid"] += 100000
            elif name == "changed_policy":
                boundary["policy_after"]["0"]["fair_sha"] = "0" * 64
            elif name == "missing_seat":
                boundary["policy_before"].pop("1")
                boundary["policy_after"].pop("1")
            elif name == "missing_stale_check":
                boundary["stale_rejected"].pop()
            elif name == "restore_state":
                boundary["restored_state"] += 1
            elif name == "restore_digest":
                boundary["restored_digest"] = "0" * 64
            elif name == "snapshot_leak":
                boundary["snapshot_released"] = False
            elif name == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0" * 64
            elif name == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0" * 64
            elif name == "unclean_engine":
                shard["cleanup"]["engines"][0]["listening"] = True
                replace_artifact(shard["job"]["name"] + "/cleanup.json", shard["cleanup"])
                replace_artifact("run-results.json", [item["cleanup"] for item in report["shards"]])
            elif name == "training_claim":
                report["training_admissible"] = True
            elif name == "fake_budget":
                entry["outcome"]["result"]["candidates"][0]["visits"] += 1
            elif name == "unfinished_backup":
                entry["outcome"]["result"]["unfinished_edge_backups"] = 1
            elif name == "short_horizon":
                native["limits"]["soft_ticks"] = 60
            elif name == "changed_target":
                entry["outcome"]["target"][0][1] += 1
            elif name == "changed_prng_during_search":
                entry["actual_prng_after"] += 1
            elif name == "missing_attempt":
                entry["attempts"].pop()
            elif name == "changed_transition":
                entry["attempts"][0]["transitions"][0]["hard_tick"] += 1
            elif name == "actual_state_copy":
                entry["audit"]["world_manifest"]["actual_state_copied"] = True
            elif name == "hidden_history":
                suffix = shard["job"]["name"] + "/history-0.json"
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/" + suffix))
                history = json.loads((root / relative).read_text())
                history["actual_prng"] = 123456
                replace_artifact(suffix, history)
            elif name == "changed_selected":
                entry["outcome"]["selected"] = "not-an-action"
            replace_artifact(shard["job"]["name"] + "/boundary.json", boundary)
            replace_artifact(shard["job"]["name"] + "/report.json", native)
            (temporary / f"native-prng-{phase}-report.json").write_text(json.dumps(report))
            try:
                validate_prng_evidence(temporary, workspace, phase)
            except ValueError as error:
                results.append(dict(case=name, rejected=True, reason=str(error)))
            else:
                raise AssertionError("PRNG validator accepted " + name)
    result = dict(status="passed", phase=phase, positive_shards=len(baseline["shards"]),
        negative_cases=results, report_sha256=hashlib.sha256((root / f"native-prng-{phase}-report.json").read_bytes()).hexdigest())
    (root / f"prng-{phase}-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
