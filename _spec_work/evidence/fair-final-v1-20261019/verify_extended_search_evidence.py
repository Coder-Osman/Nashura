from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys


def validate_extended_search_evidence(root, workspace):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from native_runner.training.fair_final.codec import ActionCodec
    from native_runner.training.fair_final.history import FairHistory
    from native_runner.training.fair_final.search import SearchLimits
    from native_runner.training.fair_search.config import HOG_DECK
    from native_runner.training.fair_search.contracts import require_sha, semantic_digest
    from run_extended_search_validation import JOBS, ENGINE_SHA

    report = json.loads((root / "native-extended-search-report.json").read_text())
    if (report["schema"] != "fair-final-extended-search-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or not report["source_sha256"] or not report["artifacts"]):
        raise ValueError("extended search suite identity differs")
    if hashlib.sha256((root / "run_extended_search_validation.py").read_bytes()).hexdigest() != report["runner_sha256"]:
        raise ValueError("extended search runner identity differs")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("extended search source changed")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("extended search artifact changed")

    def artifact(suffix):
        paths = [root / relative for relative in report["artifacts"] if relative.endswith("/" + suffix)]
        if len(paths) != 1:
            raise ValueError(f"missing unique extended artifact: {suffix}")
        return json.loads(paths[0].read_text())

    if artifact("project/probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("extended search deployment manifest differs")
    attempt = artifact("attempt.json")
    if (attempt["exit_code"] != 0 or attempt["source_sha256"] != report["source_sha256"]
            or attempt["jobs"] != JOBS or attempt["horizon"] != "default-six-groups-200-ticks"):
        raise ValueError("extended search registered coverage differs")
    preflight = artifact("preflight.json")
    if preflight["engine_sha256"] != ENGINE_SHA or preflight["memory_available"] < 64 * 1024**3 or preflight["free_bytes"] < 5 * 1024**3:
        raise ValueError("extended search resource or engine identity differs")
    run_results = artifact("run-results.json")
    if (len(run_results) != 4 or [entry["job"] for entry in run_results] != JOBS
            or any(entry["exit_code"] != 0 or entry["error"] is not None or entry["clean"] is not True for entry in run_results)):
        raise ValueError("extended search shard failed")
    if len(report["shards"]) != 4 or [shard["job"] for shard in report["shards"]] != JOBS:
        raise ValueError("extended search stage or seat coverage missing")
    child_pids = set()
    for shard in report["shards"]:
        job, native_report, cleanup = shard["job"], shard["report"], shard["cleanup"]
        if artifact(job["name"] + "/report.json") != native_report:
            raise ValueError("extended search summary differs from raw probe")
        if (native_report["schema"] != "fair-final-native-extended-search.v1"
                or native_report["status"] != "passed" or native_report["training_admissible"] is not False
                or any(native_report[key] is not True for key in ("model_unchanged", "actual_root_restored", "snapshot_released"))
                or native_report["source_sha256"] != report["source_sha256"]
                or native_report["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
                or native_report["threshold"] != job["threshold"] or native_report["owner"] != job["owner"]
                or SearchLimits(**native_report["limits"]) != SearchLimits()):
            raise ValueError("extended native identity or default horizon differs")
        limits = SearchLimits()
        records = native_report["records"]
        if len(records) != 2 or [entry["altered"] for entry in records] != [False, True]:
            raise ValueError("extended hidden mutation pair missing")
        for entry in records:
            history = FairHistory.from_payload(artifact(job["name"] + f"/history-{int(entry['altered'])}.json"))
            result, audit = entry["outcome"]["result"], entry["audit"]
            manifest = audit["world_manifest"]
            candidates = result["candidates"]
            visits = {candidate["action_id"]: candidate["visits"] for candidate in candidates}
            codec = ActionCodec(HOG_DECK)
            selected = [identity for identity, count in visits.items() if count > 0 and codec.decode(identity).key == entry["outcome"]["selected"]]
            for key in ("actual_digest_before", "actual_digest_after", "fair_sha", "history_sha"):
                require_sha(entry[key])
            if (entry["owner"] != job["owner"] or entry["threshold"] != job["threshold"]
                    or not job["threshold"] <= entry["tick"] <= job["threshold"] + 60
                    or history.root_tick != entry["tick"] or history.opening.owner != entry["owner"]
                    or history.sha256 != entry["history_sha"] or history.root_fair_sha != entry["fair_sha"]
                    or not history.decisions or not history.deployments
                    or entry["actual_digest_before"] != entry["actual_digest_after"] or entry["lease_released"] is not True
                    or result["valid_simulations"] != 128 or result["unfinished_edge_backups"] != 0
                    or type(result["invalid_attempts"]) is not int
                    or not 0 <= result["invalid_attempts"] < limits.maximum_invalid_attempts
                    or result["root_player"] != entry["owner"] or result["value_perspective"] != entry["owner"]
                    or result["root_fair_state_sha"] != history.root_fair_sha
                    or result["search_protocol_sha"] != limits.sha256 or result["noise_fraction"] != 0
                    or not 1 <= len(candidates) <= 32 or len(visits) != len(candidates)
                    or any(type(count) is not int or count < 0 for count in visits.values())
                    or sum(visits.values()) != 128 or len(selected) != 1
                    or entry["outcome"]["target"] != [[identity, visits[identity]] for identity in sorted(visits) if visits[identity]]
                    or manifest["source"] != "independent-belief" or manifest["actual_state_copied"] is not False
                    or manifest["training_admissible"] is not False
                    or manifest["root_sampling"] != "fresh-independent-world-per-build"
                    or manifest["root_fair_sha"] != history.root_fair_sha or manifest["history_sha"] != history.sha256
                    or manifest["sampler_sha"] != result["world_sampler_sha"]
                    or manifest["opponent_checkpoint_sha"] != native_report["checkpoint_sha256"]
                    or manifest["opponent_controller_sha"] != result["opponent_controller_sha"]
                    or semantic_digest(manifest) != result["world_commitment"]):
                raise ValueError("extended root budget, FAIR history or independent world differs")
            if any(not math.isfinite(candidate["q"]) or not -1 <= candidate["q"] <= 1 for candidate in candidates):
                raise ValueError("extended root-relative value differs")
            if entry["altered"]:
                mutation = entry["mutation"]
                if (mutation["before_digest"] == mutation["after_digest"]
                        or mutation["after_digest"] != entry["actual_digest_before"]):
                    raise ValueError("extended true hidden state was not changed")
            elif entry["mutation"] is not None:
                raise ValueError("unexpected baseline hidden mutation")
            attempts = entry["attempts"]
            valid = discarded = 0
            lifecycle = Counter()
            for simulation in attempts:
                ticks = decisions = 0
                previous_sha = entry["fair_sha"]
                transitions = simulation["transitions"]
                if not transitions:
                    raise ValueError("empty extended search attempt")
                for index, transition in enumerate(transitions):
                    if (decisions >= limits.soft_decisions or ticks >= limits.soft_ticks
                            or transition["before_tick"] != entry["tick"] + ticks
                            or transition["before_sha"] != previous_sha
                            or transition["hard_tick"] != entry["tick"] + limits.soft_ticks + limits.extension_ticks
                            or transition["maximum_decisions"] != limits.soft_decisions + limits.extension_decisions - decisions
                            or type(transition["completed"]) is not bool
                            or type(transition["ticks"]) is not int or transition["ticks"] < 0
                            or type(transition["decisions"]) is not int or transition["decisions"] < 0):
                        raise ValueError("extended attempt limits or boundary chain differs")
                    if not transition["completed"]:
                        discarded += 1
                        if (index != len(transitions) - 1 or transition["terminal"] is not None
                                or transition["after_sha"] != previous_sha
                                or transition["after_tick"] != transition["before_tick"]):
                            raise ValueError("unfinished extended edge was not restored")
                        continue
                    if transition["ticks"] < 1 or transition["decisions"] < 1:
                        raise ValueError("completed extended edge did not advance")
                    if transition["reason"] not in ("executed", "preempted", "rejected", "wait_expired", "terminal_expired"):
                        raise ValueError("extended completed edge lacks lifecycle")
                    ticks += transition["ticks"]
                    decisions += transition["decisions"]
                    lifecycle[transition["reason"]] += 1
                    if ticks > limits.soft_ticks + limits.extension_ticks or decisions > limits.soft_decisions + limits.extension_decisions:
                        raise ValueError("extended hard horizon exceeded")
                    if transition["terminal"] is None:
                        if transition["after_tick"] != entry["tick"] + ticks:
                            raise ValueError("extended completed boundary tick differs")
                        require_sha(transition["after_sha"])
                        previous_sha = transition["after_sha"]
                    elif (transition["terminal"] not in (-1., 0., 1.) or transition["after_sha"] is not None
                            or transition["after_tick"] is not None or index != len(transitions) - 1):
                        raise ValueError("extended terminal continuation differs")
                if simulation["ticks"] != ticks or simulation["decisions"] != decisions:
                    raise ValueError("extended attempt totals differ")
                valid += int(decisions > 0)
            if (valid != 128 or len(attempts) != audit["attempts"] or len(attempts) - valid != result["invalid_attempts"]
                    or discarded != audit["discarded_edges"] or dict(lifecycle) != audit["completed_lifecycle"]
                    or not any(simulation["decisions"] > 2 or simulation["ticks"] > 60 for simulation in attempts)):
                raise ValueError("extended effective budget or exercised horizon differs")
        for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts"):
            if records[0][key] != records[1][key]:
                raise ValueError("hidden state changed extended search result")
        if records[0]["actual_digest_before"] != records[1]["mutation"]["before_digest"]:
            raise ValueError("extended hidden mutation started from another root")
        if (artifact(job["name"] + "/cleanup.json") != cleanup or cleanup["exit_code"] != 0
                or cleanup["error"] is not None or len(cleanup["engines"]) != 2
                or [engine["port"] for engine in cleanup["engines"]] != job["ports"]):
            raise ValueError("extended search cleanup coverage differs")
        for engine in cleanup["engines"]:
            if (engine["listening"] or engine["process"]["phase"] != "stopped"
                    or set(engine["pids_alive"]) != {"pid", "child_pid"} or any(engine["pids_alive"].values())
                    or engine["process"]["port"] != engine["port"]
                    or engine["process"]["outputs"]["libresident.so"] != ENGINE_SHA):
                raise ValueError("extended engine was not cleaned")
            child_pids.add(engine["process"]["child_pid"])
    if len(child_pids) != 8:
        raise ValueError("extended shards shared native engines")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    validate_extended_search_evidence(root, root.parents[2])
    print(json.dumps(dict(status="passed", roots=8, effective_simulations=1024, default_horizon=True)))
