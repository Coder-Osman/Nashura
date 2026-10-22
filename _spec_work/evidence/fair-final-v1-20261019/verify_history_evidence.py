import hashlib
import json
import math
from pathlib import Path
import sys


def validate_history_evidence(root, workspace):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from native_runner.training.fair_final.history import FairHistory

    report = json.loads((root / "native-history-report.json").read_text())
    if (report["schema"] != "fair-final-native-history-acceptance.v1"
            or report["status"] != "passed" or report["training_admissible"] is not False
            or report["model_unchanged"] is not True
            or report["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"):
        raise ValueError("native history acceptance identity differs")
    if hashlib.sha256((root / "run_history_validation.py").read_bytes()).hexdigest() != report["runner_sha256"]:
        raise ValueError("native history runner identity differs")
    if not report["source_sha256"] or not report["artifacts"]:
        raise ValueError("missing history source or artifact identity")
    for relative, expected in report["source_sha256"].items():
        path = workspace / "firstlight_CR" / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"native history source changed: {relative}")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"native history artifact changed: {relative}")

    def artifact(name):
        matches = [root / relative for relative in report["artifacts"] if Path(relative).name == name]
        if len(matches) != 1:
            raise ValueError(f"missing unique history artifact: {name}")
        return json.loads(matches[0].read_text())

    if artifact("report.json") != {key: value for key, value in report.items()
            if key not in ("runner_sha256", "artifacts", "cleanup")}:
        raise ValueError("history summary differs from raw probe")
    if artifact("probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("history deployment manifest differs")
    attempt = artifact("attempt.json")
    if (attempt["exit_code"] != 0 or attempt["source_sha256"] != report["source_sha256"]
            or attempt["incremental"] is not True or attempt["thresholds"] != [1200, 3600, 5400]):
        raise ValueError("history probe lacks registered incremental coverage")
    records = report["records"]
    if len(records) != 6 or {(entry["threshold"], entry["owner"]) for entry in records} != {
            (threshold, owner) for threshold in (1200, 3600, 5400) for owner in (0, 1)}:
        raise ValueError("history both-seat stage coverage missing")
    for entry in records:
        threshold, owner = entry["threshold"], entry["owner"]
        history = FairHistory.from_payload(artifact(f"history-{threshold}-{owner}.json"))
        following = FairHistory.from_payload(artifact(f"history-{threshold}-{owner}-incremental.json"))
        profile, cold = entry["profile"], entry["cold_profile"]
        delta = following.root_tick - history.root_tick
        if (entry["exact"] is not True or entry["incremental"] is not True
                or entry["actual_match_unchanged"] is not True or entry["tick"] < threshold
                or history.root_tick != entry["tick"] or history.opening.owner != owner
                or history.sha256 != entry["history_sha"] or not following.extends(history)
                or following.root_tick != entry["next_tick"] or following.root_fair_sha != entry["next_fair_sha"]
                or delta <= 0 or len(history.decisions) != entry["own_decisions"]
                or len(history.deployments) != entry["public_deployments"]
                or cold["syncs"] != 1 or cold["incremental_ticks"] != 0
                or cold["cold_ticks"] != history.root_tick - history.opening.tick
                or cold["ticks"] != cold["cold_ticks"]
                or profile["syncs"] != 2 or profile["cold_ticks"] != cold["cold_ticks"]
                or profile["incremental_ticks"] != delta
                or profile["ticks"] != following.root_tick - history.opening.tick
                or profile["last_sync"]["from_tick"] != history.root_tick
                or profile["last_sync"]["to_tick"] != following.root_tick
                or profile["last_sync"]["ticks"] != delta):
            raise ValueError("native history root, prefix or incremental replay evidence differs")
        for seconds in (profile["seconds"], cold["seconds"], profile["last_sync"]["seconds"]):
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("invalid history timings")
    cleanup = report["cleanup"]
    if (artifact("cleanup.json") != cleanup or cleanup["exit_code"] != 0
            or len(cleanup["engines"]) != 2
            or {entry["port"] for entry in cleanup["engines"]} != {29626, 29627}
            or len({entry["process"]["child_pid"] for entry in cleanup["engines"]}) != 2
            or any(entry["process"]["phase"] != "stopped" or entry["listening"]
                or any(entry["pids_alive"].values()) for entry in cleanup["engines"])):
        raise ValueError("history independent engines lack cleanup evidence")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    report = validate_history_evidence(root, root.parents[2])
    print(json.dumps(dict(status="passed", checks=len(report["records"]), seconds=report["seconds"])))
