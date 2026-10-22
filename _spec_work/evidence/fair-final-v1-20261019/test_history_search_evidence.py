from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_history_search_evidence import validate_history_search_evidence


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    baseline = validate_history_search_evidence(root, workspace)
    cases = ("missing_seat", "fake_budget", "changed_root", "hidden_input",
        "same_world_across_roots", "unchanged_hidden_state", "changed_source",
        "changed_artifact", "unclean_engine", "fake_match_unchanged", "missing_recovery")
    results = []
    for name in cases:
        with tempfile.TemporaryDirectory(prefix="fair-history-search-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to((root / relative).resolve())
            (temporary / "run_history_search_validation.py").symlink_to(root / "run_history_search_validation.py")

            def replace_artifact(filename, payload):
                relative = next(relative for relative in report["artifacts"] if Path(relative).name == filename)
                target = temporary / relative
                if target.is_symlink():
                    target.unlink()
                target.write_text(json.dumps(payload, sort_keys=True))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            if name == "missing_seat":
                report["records"].pop()
            elif name == "fake_budget":
                report["records"][0]["outcome"]["result"]["candidates"][0]["visits"] += 1
            elif name == "changed_root":
                report["records"][0]["fair_sha"] = "0" * 64
            elif name == "hidden_input":
                filename = "history-root-0-0.json"
                relative = next(relative for relative in report["artifacts"] if Path(relative).name == filename)
                payload = json.loads((root / relative).read_text())
                payload["actual_match_seed"] = 12345
                replace_artifact(filename, payload)
            elif name == "same_world_across_roots":
                report["records"][0]["audit"]["world_manifest"]["root_sampling"] = "reuse-old-world"
            elif name == "unchanged_hidden_state":
                entry = next(entry for entry in report["records"] if entry["altered"])
                entry["mutation"]["after_digest"] = entry["mutation"]["before_digest"]
            elif name == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0" * 64
            elif name == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0" * 64
            elif name == "unclean_engine":
                report["cleanup"]["engines"][0]["listening"] = True
                replace_artifact("cleanup.json", report["cleanup"])
            elif name == "fake_match_unchanged":
                report["records"][0]["actual_match_unchanged"] = False
            elif name == "missing_recovery":
                report["recovery"].pop()
            replace_artifact("report.json", {key: value for key, value in report.items()
                if key not in ("runner_sha256", "artifacts", "cleanup")})
            (temporary / "native-history-search-report.json").write_text(json.dumps(report))
            try:
                validate_history_search_evidence(temporary, workspace)
            except ValueError as error:
                results.append(dict(case=name, rejected=True, reason=str(error)))
            else:
                raise AssertionError(f"history search validator accepted {name}")
    result = dict(status="passed", positive_checks=len(baseline["records"]), negative_cases=results,
        report_sha256=hashlib.sha256((root / "native-history-search-report.json").read_bytes()).hexdigest())
    (root / "history-search-evidence-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
