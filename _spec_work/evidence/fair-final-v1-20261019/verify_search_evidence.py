import hashlib
import json
import math
from pathlib import Path


def validate_search_evidence(root, workspace):
    report = json.loads((root/"native-search-report.json").read_text())
    if (report["schema"] != "fair-final-native-search-acceptance.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["model_unchanged"] is not True
            or report["actual_match_unchanged"] is not True
            or report["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"):
        raise ValueError("native search acceptance identity differs")
    if hashlib.sha256((root/"run_search_validation.py").read_bytes()).hexdigest() != report["runner_sha256"]:
        raise ValueError("native search runner identity differs")
    if not report["source_sha256"] or not report["artifacts"]:
        raise ValueError("missing native search source or artifact identity")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace/"firstlight_CR"/relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"native search source changed: {relative}")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"native search artifact changed: {relative}")
    raw_paths = [root/relative for relative in report["artifacts"] if Path(relative).name == "report.json"]
    if len(raw_paths) != 1 or json.loads(raw_paths[0].read_text()) != {key: value for key, value in report.items()
            if key not in ("runner_sha256", "artifacts", "cleanup")}:
        raise ValueError("native search summary differs from raw evidence")
    for name, expected in (("cleanup.json", report["cleanup"]),
            ("probe-source-manifest.json", report["source_sha256"])):
        matches = [root/relative for relative in report["artifacts"] if Path(relative).name == name]
        if len(matches) != 1 or json.loads(matches[0].read_text()) != expected:
            raise ValueError(f"native search raw evidence differs: {name}")
    records = report["records"]
    if len(records) != 4 or {(entry["owner"], entry["altered"]) for entry in records} != {
            (owner, altered) for owner in (0, 1) for altered in (False, True)}:
        raise ValueError("native search both-seat perturbation coverage missing")
    for owner in (0, 1):
        before, after = sorted((entry for entry in records if entry["owner"] == owner), key=lambda entry: entry["altered"])
        mutation = after["noninterference"]["native_mutation"]
        if (before["outcome"] != after["outcome"] or after["noninterference"]["exact"] is not True
                or mutation["before_digest"] == mutation["after_digest"]):
            raise ValueError("native hidden-state noninterference failed")
        for entry in (before, after):
            result = entry["outcome"]["result"]
            candidates = result["candidates"]
            if (not 1 <= len(candidates) <= 32 or len({candidate["action_id"] for candidate in candidates}) != len(candidates)
                    or any(type(candidate["visits"]) is not int or candidate["visits"] < 0
                        or not math.isfinite(candidate["q"]) or not -1 <= candidate["q"] <= 1 for candidate in candidates)
                    or not math.isclose(sum(candidate["prior_projected"] for candidate in candidates), 1., abs_tol=1e-5)
                    or any(candidate["prior_projected"] != candidate["prior_search"] for candidate in candidates)):
                raise ValueError("native search candidate statistics differ")
            target = [[candidate["action_id"], candidate["visits"]]
                for candidate in sorted(candidates, key=lambda candidate: candidate["action_id"]) if candidate["visits"]]
            if (result["schema"] != "SearchResult/v1" or result["root_player"] != owner
                    or result["value_perspective"] != owner or result["valid_simulations"] != 128
                    or result["unfinished_edge_backups"] != 0 or result["noise_fraction"] != 0
                    or result["search_protocol_sha"] != report["search_protocol_sha"]
                    or sum(candidate["visits"] for candidate in candidates) != 128
                    or entry["outcome"]["target"] != target
                    or entry["audit"]["attempts"] != 128+result["invalid_attempts"]
                    or entry["audit"]["world_manifest"]["source"] != "independent-belief"
                    or entry["audit"]["world_manifest"]["root_fair_sha"] != result["root_fair_state_sha"]
                    or entry["audit"]["world_manifest"]["actual_state_copied"] is not False):
                raise ValueError("effective Full128 search evidence differs")
    cleanup = report["cleanup"]
    if (cleanup["exit_code"] != 0 or len(cleanup["engines"]) != 2
            or {entry["port"] for entry in cleanup["engines"]} != {29624, 29625}
            or len({entry["process"]["child_pid"] for entry in cleanup["engines"]}) != 2
            or any(entry["process"]["phase"] != "stopped" or entry["listening"]
                or any(entry["pids_alive"].values()) for entry in cleanup["engines"])):
        raise ValueError("independent native engines lack cleanup evidence")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    report = validate_search_evidence(root, root.parents[2])
    print(json.dumps(dict(status="passed", roots=len(report["records"]), seconds=report["seconds"])))
