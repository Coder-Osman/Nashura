from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys


def validate_prng_evidence(root, workspace, phase="full"):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from native_runner.training.fair_final.codec import ActionCodec
    from native_runner.training.fair_final.history import FairHistory
    from native_runner.training.fair_final.search import SearchLimits
    from native_runner.training.fair_search.config import HOG_DECK
    from native_runner.training.fair_search.contracts import require_sha, semantic_digest
    from tools.fair_final_prng_audit import LIBG_SHA, PROBE_SHA, next_native_state
    from tools.probe_fair_final_extended_search import SOURCE_SHA
    from run_prng_validation import JOBS

    report = json.loads((root / f"native-prng-{phase}-report.json").read_text())
    jobs = JOBS if phase == "full" else JOBS[:1]
    if (report["schema"] != "fair-final-prng-suite.v1" or report["status"] != "passed"
            or report["phase"] != phase or report["training_admissible"] is not False
            or not report["source_sha256"] or not report["artifacts"]):
        raise ValueError("PRNG suite identity differs")
    for name, key in (("run_prng_validation.py", "runner_sha256"), ("prng_validation_worker.py", "worker_sha256")):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("PRNG runner or worker identity differs")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("PRNG source changed")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("PRNG artifact changed")

    def artifact(suffix):
        paths = [root / relative for relative in report["artifacts"] if relative.endswith("/" + suffix)]
        if len(paths) != 1:
            raise ValueError("missing unique PRNG artifact: " + suffix)
        return json.loads(paths[0].read_text())

    def mutation_valid(mutation):
        if (mutation["probe_sha256"] != PROBE_SHA or mutation["libg_sha256"] != LIBG_SHA
                or mutation["manager_offset"] != "0xa0"
                or mutation["algorithm"] != "native-xorshift32-arithmetic-shift"
                or mutation["changed"] is not True or mutation["raw_unchanged"] is not True
                or mutation["after"] != next_native_state(mutation["before"])
                or mutation["before"] == mutation["after"]
                or mutation["before_digest"] == mutation["after_digest"]):
            raise ValueError("native PRNG perturbation evidence differs")
        for key in ("before_digest", "after_digest"):
            require_sha(mutation[key])

    if artifact("project/probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("PRNG deployment source differs")
    worker_artifacts = [digest for relative, digest in report["artifacts"].items()
        if relative.endswith("/project/run.py")]
    if worker_artifacts != [report["worker_sha256"]]:
        raise ValueError("PRNG deployed worker differs from local runner")
    attempt = artifact("attempt.json")
    registration = artifact("run-manifest.json")
    if (attempt["exit_code"] != 0 or attempt["jobs"] != jobs or attempt["phase"] != phase
            or attempt["source_sha256"] != report["source_sha256"]
            or attempt["worker_sha256"] != report["worker_sha256"]
            or registration["jobs"] != jobs or registration["phase"] != phase):
        raise ValueError("PRNG registered attempt differs")
    preflight = artifact("preflight.json")
    if (preflight["engine_sha256"] != PROBE_SHA or preflight["memory_available"] < 64*1024**3
            or preflight["free_bytes"] < 5*1024**3):
        raise ValueError("PRNG preflight resource or binary identity differs")
    if [entry["job"] for entry in report["shards"]] != jobs:
        raise ValueError("PRNG stage or seat coverage missing")
    if artifact("run-results.json") != [entry["cleanup"] for entry in report["shards"]]:
        raise ValueError("PRNG cleanup aggregate differs")
    processes = set()
    for shard in report["shards"]:
        job, native, cleanup = shard["job"], shard["report"], shard["cleanup"]
        limits = SearchLimits()
        if (artifact(job["name"] + "/report.json") != native
                or native["schema"] != "fair-final-native-prng.v1" or native["status"] != "passed"
                or native["training_admissible"] is not False or native["checkpoint_sha256"] != SOURCE_SHA
                or native["source_sha256"] != report["source_sha256"]
                or native["threshold"] != job["threshold"] or native["owner"] != job["owner"]
                or native["smoke_only"] != (phase == "smoke") or SearchLimits(**native["limits"]) != limits
                or any(native[key] is not True for key in ("model_unchanged", "actual_root_restored", "snapshot_released"))):
            raise ValueError("PRNG native identity or horizon differs")
        boundary = native["boundary"]
        mutation_valid(boundary["mutation"])
        if (artifact(job["name"] + "/boundary.json") != boundary
                or boundary["policy_before"] != boundary["policy_after"]
                or set(boundary["policy_before"]) != {"0", "1"}
                or boundary["restored_state"] != boundary["mutation"]["before"]
                or boundary["restored_digest"] != boundary["mutation"]["before_digest"]
                or boundary["snapshot_released"] is not True
                or boundary["stale_rejected"] != ["generation", "stateEpoch", "tick", "expected_state"]):
            raise ValueError("PRNG boundary policy, restoration or stale rejection differs")
        if not job["threshold"] <= boundary["mutation"]["tick"] <= job["threshold"] + 60:
            raise ValueError("PRNG boundary lies outside registered stage")
        records = native["records"]
        if [entry["altered"] for entry in records] != ([] if phase == "smoke" else [False, True]):
            raise ValueError("PRNG paired root coverage differs")
        for entry in records:
            history = FairHistory.from_payload(artifact(job["name"] + f"/history-{int(entry['altered'])}.json"))
            result, audit = entry["outcome"]["result"], entry["audit"]
            manifest = audit["world_manifest"]
            candidates = result["candidates"]
            codec = ActionCodec(HOG_DECK)
            visits = {candidate["action_id"]: candidate["visits"] for candidate in candidates}
            selected = [identity for identity, count in visits.items()
                if count > 0 and codec.decode(identity).key == entry["outcome"]["selected"]]
            if (not job["threshold"] <= entry["tick"] <= job["threshold"] + 60
                    or entry["owner"] != job["owner"] or history.root_tick != entry["tick"]
                    or history.opening.owner != entry["owner"] or history.sha256 != entry["history_sha"]
                    or history.root_fair_sha != entry["fair_sha"]
                    or entry["fair_sha"] != boundary["policy_before"][str(job["owner"])]["fair_sha"]
                    or result["root_fair_state_sha"] != entry["fair_sha"]
                    or entry["actual_digest_before"] != entry["actual_digest_after"]
                    or entry["actual_prng_before"] != entry["actual_prng_after"]
                    or entry["lease_released"] is not True or result["valid_simulations"] != 128
                    or result["unfinished_edge_backups"] != 0 or not 0 <= result["invalid_attempts"] < limits.maximum_invalid_attempts
                    or result["root_player"] != job["owner"] or result["value_perspective"] != job["owner"]
                    or result["search_protocol_sha"] != limits.sha256 or result["noise_fraction"] != 0
                    or not 1 <= len(visits) == len(candidates) <= 32 or sum(visits.values()) != 128
                    or len(selected) != 1 or any(type(count) is not int or count < 0 for count in visits.values())
                    or entry["outcome"]["target"] != [[identity, visits[identity]] for identity in sorted(visits) if visits[identity]]
                    or any(not math.isfinite(candidate["q"]) or not -1 <= candidate["q"] <= 1 for candidate in candidates)
                    or manifest["source"] != "independent-belief" or manifest["actual_state_copied"] is not False
                    or manifest["training_admissible"] is not False
                    or manifest["root_sampling"] != "fresh-independent-world-per-build"
                    or manifest["root_fair_sha"] != entry["fair_sha"] or manifest["history_sha"] != history.sha256
                    or manifest["sampler_sha"] != result["world_sampler_sha"]
                    or manifest["opponent_checkpoint_sha"] != SOURCE_SHA
                    or manifest["opponent_controller_sha"] != result["opponent_controller_sha"]
                    or semantic_digest(manifest) != result["world_commitment"]):
                raise ValueError("PRNG search root, budget, labels or independent belief differs")
            if entry["altered"]:
                mutation_valid(entry["mutation"])
                if (entry["mutation"]["before_digest"] != records[0]["actual_digest_before"]
                        or entry["mutation"]["after_digest"] != entry["actual_digest_before"]
                        or entry["mutation"]["before"] != records[0]["actual_prng_before"]
                        or entry["mutation"]["after"] != entry["actual_prng_before"]
                        or entry["mutation"]["tick"] != entry["tick"]
                        or entry["mutation"]["child_pid"] != boundary["mutation"]["child_pid"]
                        or entry["mutation"]["child_start_ticks"] != boundary["mutation"]["child_start_ticks"]):
                    raise ValueError("PRNG search did not use the perturbed actual root")
            elif entry["mutation"] is not None or entry["actual_prng_before"] != boundary["restored_state"]:
                raise ValueError("PRNG baseline root differs")
            valid = discarded = 0
            lifecycle = Counter()
            for simulation in entry["attempts"]:
                ticks = decisions = 0
                previous_sha = entry["fair_sha"]
                transitions = simulation["transitions"]
                if not transitions:
                    raise ValueError("empty PRNG simulation")
                for index, transition in enumerate(transitions):
                    if (ticks >= limits.soft_ticks or decisions >= limits.soft_decisions
                            or transition["before_tick"] != entry["tick"] + ticks or transition["before_sha"] != previous_sha
                            or transition["hard_tick"] != entry["tick"] + limits.soft_ticks + limits.extension_ticks
                            or transition["maximum_decisions"] != limits.soft_decisions + limits.extension_decisions - decisions):
                        raise ValueError("PRNG simulation boundary chain differs")
                    if (type(transition["completed"]) is not bool
                            or type(transition["ticks"]) is not int or transition["ticks"] < 0
                            or type(transition["decisions"]) is not int or transition["decisions"] < 0):
                        raise ValueError("PRNG simulation boundary types differ")
                    if not transition["completed"]:
                        discarded += 1
                        if (index != len(transitions)-1 or transition["terminal"] is not None
                                or transition["after_tick"] != transition["before_tick"] or transition["after_sha"] != previous_sha):
                            raise ValueError("PRNG unfinished boundary was not restored")
                        continue
                    if transition["ticks"] < 1 or transition["decisions"] < 1:
                        raise ValueError("PRNG completed edge did not advance")
                    if transition["reason"] not in ("executed", "preempted", "rejected", "wait_expired", "terminal_expired"):
                        raise ValueError("PRNG completed edge lacks lifecycle")
                    ticks += transition["ticks"]
                    decisions += transition["decisions"]
                    lifecycle[transition["reason"]] += 1
                    if ticks > 260 or decisions > 12:
                        raise ValueError("PRNG simulation exceeded hard horizon")
                    if transition["terminal"] is None:
                        require_sha(transition["after_sha"])
                        if transition["after_tick"] != entry["tick"] + ticks:
                            raise ValueError("PRNG completed tick differs")
                        previous_sha = transition["after_sha"]
                    elif (transition["terminal"] not in (-1., 0., 1.) or transition["after_sha"] is not None
                            or transition["after_tick"] is not None or index != len(transitions)-1):
                        raise ValueError("PRNG terminal boundary differs")
                if simulation["ticks"] != ticks or simulation["decisions"] != decisions:
                    raise ValueError("PRNG simulation totals differ")
                valid += int(decisions > 0)
            if (valid != 128 or len(entry["attempts"]) != audit["attempts"]
                    or len(entry["attempts"]) - valid != result["invalid_attempts"]
                    or discarded != audit["discarded_edges"] or dict(lifecycle) != audit["completed_lifecycle"]):
                raise ValueError("PRNG effective simulation audit differs")
        if records:
            for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts"):
                if records[0][key] != records[1][key]:
                    raise ValueError("native PRNG changed search output or trajectory")
        if (artifact(job["name"] + "/cleanup.json") != cleanup or cleanup["job"] != job
                or cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
                or [engine["port"] for engine in cleanup["engines"]] != job["ports"]):
            raise ValueError("PRNG cleanup coverage differs")
        for engine in cleanup["engines"]:
            process = engine["process"]
            if (process["phase"] != "stopped" or engine["listening"] or any(engine["pids_alive"].values())
                    or process["outputs"]["libresident.so"] != PROBE_SHA
                    or artifact(job["name"] + f"/engine-{engine['port']}/process.json") != process):
                raise ValueError("PRNG engine cleanup differs")
            processes.add(process["child_pid"])
        actual = cleanup["engines"][0]["process"]
        if (boundary["mutation"]["child_pid"] != actual["child_pid"]
                or str(boundary["mutation"]["child_start_ticks"]) != str(actual["child_start_ticks"])):
            raise ValueError("PRNG mutation belongs to another process")
    if len(processes) != 2*len(jobs):
        raise ValueError("PRNG shards shared engines")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    phase = sys.argv[1] if len(sys.argv) > 1 else "full"
    result = validate_prng_evidence(root, root.parents[2], phase)
    print(json.dumps(dict(status="passed", phase=phase, shards=len(result["shards"]))))
