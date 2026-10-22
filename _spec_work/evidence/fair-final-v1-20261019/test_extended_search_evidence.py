from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_extended_search_evidence import validate_extended_search_evidence


NEGATIVE_CASES = ("missing_stage", "short_horizon", "fake_budget", "hidden_input",
    "changed_fair_root", "actual_state_copy", "unchanged_hidden", "changed_actual_match",
    "missing_attempt", "changed_transition", "hard_horizon", "unfinished_backup",
    "changed_target", "changed_source", "changed_artifact", "unclean_engine", "training_claim")


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    baseline = validate_extended_search_evidence(root, workspace)
    results = []
    for name in NEGATIVE_CASES:
        with tempfile.TemporaryDirectory(prefix="fair-extended-search-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to((root / relative).resolve())
            (temporary / "run_extended_search_validation.py").symlink_to(root / "run_extended_search_validation.py")

            def replace_artifact(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/" + suffix))
                target = temporary / relative
                if target.is_symlink():
                    target.unlink()
                target.write_text(json.dumps(payload, sort_keys=True))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            shard = report["shards"][0]
            native = shard["report"]
            entry = native["records"][0]
            if name == "missing_stage":
                report["shards"].pop()
            elif name == "short_horizon":
                native["limits"]["soft_decisions"] = 2
                native["limits"]["soft_ticks"] = 60
            elif name == "fake_budget":
                entry["outcome"]["result"]["candidates"][0]["visits"] += 1
            elif name == "hidden_input":
                suffix = shard["job"]["name"] + "/history-0.json"
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/" + suffix))
                history = json.loads((root / relative).read_text())
                history["actual_match_seed"] = 78101961
                replace_artifact(suffix, history)
            elif name == "changed_fair_root":
                entry["fair_sha"] = "0" * 64
            elif name == "actual_state_copy":
                entry["audit"]["world_manifest"]["actual_state_copied"] = True
            elif name == "unchanged_hidden":
                mutation = native["records"][1]["mutation"]
                mutation["after_digest"] = mutation["before_digest"]
            elif name == "changed_actual_match":
                entry["actual_digest_after"] = "0" * 64
            elif name == "missing_attempt":
                entry["attempts"].pop()
            elif name == "changed_transition":
                entry["attempts"][0]["transitions"][0]["after_tick"] += 1
            elif name == "hard_horizon":
                entry["attempts"][0]["transitions"][0]["hard_tick"] += 1
            elif name == "unfinished_backup":
                entry["outcome"]["result"]["unfinished_edge_backups"] = 1
            elif name == "changed_target":
                entry["outcome"]["target"][0][1] += 1
            elif name == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0" * 64
            elif name == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0" * 64
            elif name == "unclean_engine":
                shard["cleanup"]["engines"][0]["listening"] = True
                replace_artifact(shard["job"]["name"] + "/cleanup.json", shard["cleanup"])
            elif name == "training_claim":
                report["training_admissible"] = True
            replace_artifact(shard["job"]["name"] + "/report.json", native)
            (temporary / "native-extended-search-report.json").write_text(json.dumps(report))
            try:
                validate_extended_search_evidence(temporary, workspace)
            except ValueError as error:
                results.append(dict(case=name, rejected=True, reason=str(error)))
            else:
                raise AssertionError(f"extended search validator accepted {name}")
    result = dict(status="passed", positive_checks=8, negative_cases=results,
        report_sha256=hashlib.sha256((root / "native-extended-search-report.json").read_bytes()).hexdigest())
    (root / "extended-search-evidence-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
