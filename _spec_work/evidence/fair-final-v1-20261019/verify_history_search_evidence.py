import hashlib
import json
import math
from pathlib import Path
import sys


def validate_history_search_evidence(root, workspace):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from native_runner.training.fair_final.history import FairHistory
    from native_runner.training.fair_final.codec import ActionCodec
    from native_runner.training.fair_final.search import SearchLimits
    from native_runner.training.fair_search.config import HOG_DECK
    from native_runner.training.fair_search.contracts import semantic_digest

    report = json.loads((root / "native-history-search-report.json").read_text())
    if (report["schema"] != "fair-final-native-history-search-acceptance.v1"
            or report["status"] != "passed" or report["training_admissible"] is not False
            or report["model_unchanged"] is not True
            or report["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"):
        raise ValueError("history search acceptance identity differs")
    if hashlib.sha256((root / "run_history_search_validation.py").read_bytes()).hexdigest() != report["runner_sha256"]:
        raise ValueError("history search runner identity differs")
    if not report["source_sha256"] or not report["artifacts"]:
        raise ValueError("missing history search source or artifacts")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"history search source changed: {relative}")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"history search artifact changed: {relative}")

    def artifact(name):
        paths = [root / relative for relative in report["artifacts"] if Path(relative).name == name]
        if len(paths) != 1:
            raise ValueError(f"missing unique history search artifact: {name}")
        return json.loads(paths[0].read_text())

    if artifact("report.json") != {key: value for key, value in report.items()
            if key not in ("runner_sha256", "artifacts", "cleanup")}:
        raise ValueError("history search summary differs from raw probe")
    if artifact("probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("history search deployment manifest differs")
    attempt = artifact("attempt.json")
    if (attempt["exit_code"] != 0 or attempt["source_sha256"] != report["source_sha256"]
            or attempt["root_threshold"] != 1200 or attempt["seats"] != [0, 1]
            or attempt["modes"] != [False, True]):
        raise ValueError("registered history search coverage differs")
    limits = SearchLimits(**report["limits"])
    if limits != SearchLimits(soft_decisions=2, soft_ticks=60):
        raise ValueError("history search engineering horizon changed")
    records = report["records"]
    if len(records) != 4 or {(entry["owner"], entry["altered"]) for entry in records} != {
            (owner, altered) for owner in (0, 1) for altered in (False, True)}:
        raise ValueError("missing both-seat hidden-mutation search coverage")
    for entry in records:
        owner, altered = entry["owner"], entry["altered"]
        history = FairHistory.from_payload(artifact(f"history-root-{owner}-{int(altered)}.json"))
        result, audit = entry["outcome"]["result"], entry["audit"]
        manifest = audit["world_manifest"]
        candidates = result["candidates"]
        visits = {candidate["action_id"]: candidate["visits"] for candidate in candidates}
        codec = ActionCodec(HOG_DECK)
        selected = [identity for identity, count in visits.items()
            if count > 0 and codec.decode(identity).key == entry["outcome"]["selected"]]
        if (entry["tick"] < 1200 or history.root_tick != entry["tick"]
                or history.opening.owner != owner or history.sha256 != entry["history_sha"]
                or history.root_fair_sha != entry["fair_sha"] or not history.decisions or not history.deployments
                or entry["actual_match_unchanged"] is not True or entry["lease_released"] is not True
                or result["valid_simulations"] != 128 or result["unfinished_edge_backups"] != 0
                or result["root_player"] != owner or result["value_perspective"] != owner
                or result["root_fair_state_sha"] != history.root_fair_sha
                or result["search_protocol_sha"] != limits.sha256 or result["noise_fraction"] != 0
                or not 1 <= len(candidates) <= 32 or len(visits) != len(candidates)
                or any(type(count) is not int or count < 0 for count in visits.values())
                or sum(visits.values()) != 128
                or len(selected) != 1
                or entry["outcome"]["target"] != [[identity, visits[identity]] for identity in sorted(visits) if visits[identity]]
                or manifest["source"] != "independent-belief" or manifest["actual_state_copied"] is not False
                or manifest["training_admissible"] is not False
                or manifest["root_sampling"] != "fresh-independent-world-per-build"
                or manifest["root_fair_sha"] != history.root_fair_sha or manifest["history_sha"] != history.sha256
                or manifest["sampler_sha"] != result["world_sampler_sha"]
                or manifest["opponent_checkpoint_sha"] != report["checkpoint_sha256"]
                or manifest["opponent_controller_sha"] != result["opponent_controller_sha"]
                or semantic_digest(manifest) != result["world_commitment"]):
            raise ValueError("history search root, budget, target or world evidence differs")
        for candidate in candidates:
            if not math.isfinite(candidate["q"]) or not -1 <= candidate["q"] <= 1:
                raise ValueError("invalid root-relative value")
        if altered:
            mutation = entry["mutation"]
            if mutation["before_digest"] == mutation["after_digest"]:
                raise ValueError("actual hidden state was not changed")
        elif entry["mutation"] is not None:
            raise ValueError("unexpected baseline mutation")
    for owner in (0, 1):
        pair = [entry for entry in records if entry["owner"] == owner]
        for key in ("outcome", "history_sha", "fair_sha", "tick"):
            if pair[0][key] != pair[1][key]:
                raise ValueError("hidden mutation changed history search result")
    recovery = report["recovery"]
    if (len(recovery) != 12 or recovery[0]["mode"] != "cold"
            or not any(entry["mode"] == "rebuild" for entry in recovery)
            or not any(entry["mode"] == "incremental" for entry in recovery)):
        raise ValueError("history recovery boundary coverage missing")
    for index, entry in enumerate(recovery):
        if (entry["status"] != "verified" or entry["actual_match_unchanged"] is not True
                or entry["mode"] not in ("cold", "incremental", "rebuild")
                or not math.isfinite(entry["seconds"]) or entry["seconds"] <= 0
                or index > 0 and (entry["from_tick"] != recovery[index-1]["to_tick"]
                    or entry["to_tick"] <= entry["from_tick"])
                or entry["mode"] == "rebuild" and not entry["reason"]):
            raise ValueError("history recovery cursor evidence differs")
    cleanup = report["cleanup"]
    if (artifact("cleanup.json") != cleanup or cleanup["exit_code"] != 0
            or len(cleanup["engines"]) != 2
            or {entry["port"] for entry in cleanup["engines"]} != {29628, 29629}
            or len({entry["process"]["child_pid"] for entry in cleanup["engines"]}) != 2
            or any(entry["process"]["phase"] != "stopped" or entry["listening"]
                or any(entry["pids_alive"].values()) for entry in cleanup["engines"])):
        raise ValueError("history search engines lack cleanup evidence")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    report = validate_history_search_evidence(root, root.parents[2])
    print(json.dumps(dict(status="passed", roots=len(report["records"]),
        rebuilds=sum(entry["mode"] == "rebuild" for entry in report["recovery"]))))
