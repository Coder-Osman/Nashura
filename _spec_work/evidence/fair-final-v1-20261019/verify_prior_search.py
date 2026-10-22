from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
SOURCE = WORKSPACE/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.prior_history import HistoryCycleCursor, PriorHistoryBeliefFactory
from native_runner.training.fair_final.prior_provider import PriorHistorySearchProvider
from native_runner.training.fair_final.search import SearchLimits, split_seed
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import require_sha, semantic_digest

SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
JOBS = [dict(name=f"stage-{threshold}-seat-{owner}", threshold=threshold, owner=owner,
    ports=[29652+index*2, 29653+index*2])
    for index, (threshold, owner) in enumerate((threshold, owner) for threshold in (1200, 3600) for owner in (0, 1))]


def validate_attempts(entry, limits, *, require_depth_coverage=True):
    valid = discarded = 0
    lifecycle = Counter()
    attempts, audit, result = entry["attempts"], entry["audit"], entry["outcome"]["result"]
    for simulation in attempts:
        ticks = decisions = 0
        previous_sha = entry["fair_sha"]
        if not simulation["transitions"]:
            raise ValueError("empty prior search attempt")
        for index, transition in enumerate(simulation["transitions"]):
            if (decisions >= limits.soft_decisions or ticks >= limits.soft_ticks
                    or transition["before_tick"] != entry["tick"]+ticks
                    or transition["before_sha"] != previous_sha
                    or transition["hard_tick"] != entry["tick"]+limits.soft_ticks+limits.extension_ticks
                    or transition["maximum_decisions"] != limits.soft_decisions+limits.extension_decisions-decisions
                    or type(transition["completed"]) is not bool
                    or type(transition["ticks"]) is not int or transition["ticks"] < 0
                    or type(transition["decisions"]) is not int or transition["decisions"] < 0):
                raise ValueError("prior attempt limits or boundary chain differs")
            if not transition["completed"]:
                discarded += 1
                if (index != len(simulation["transitions"])-1 or transition["terminal"] is not None
                        or transition["after_sha"] != previous_sha
                        or transition["after_tick"] != transition["before_tick"]):
                    raise ValueError("unfinished prior edge not restored")
                continue
            if transition["ticks"] < 1 or transition["decisions"] < 1:
                raise ValueError("completed prior edge did not advance")
            if transition["reason"] not in ("executed", "preempted", "rejected", "wait_expired", "terminal_expired"):
                raise ValueError("prior edge lifecycle missing")
            ticks += transition["ticks"]
            decisions += transition["decisions"]
            lifecycle[transition["reason"]] += 1
            if ticks > limits.soft_ticks+limits.extension_ticks or decisions > limits.soft_decisions+limits.extension_decisions:
                raise ValueError("prior hard horizon exceeded")
            if transition["terminal"] is None:
                if transition["after_tick"] != entry["tick"]+ticks:
                    raise ValueError("prior completed boundary tick differs")
                require_sha(transition["after_sha"])
                previous_sha = transition["after_sha"]
            elif (transition["terminal"] not in (-1., 0., 1.) or transition["after_sha"] is not None
                    or transition["after_tick"] is not None or index != len(simulation["transitions"])-1):
                raise ValueError("prior terminal edge differs")
        if simulation["ticks"] != ticks or simulation["decisions"] != decisions:
            raise ValueError("prior attempt totals differ")
        valid += int(decisions > 0)
    if (valid != 128 or len(attempts) != audit["attempts"]
            or len(attempts)-valid != result["invalid_attempts"]
            or discarded != audit["discarded_edges"] or dict(lifecycle) != audit["completed_lifecycle"]
            or (require_depth_coverage and not any(
                attempt["decisions"] > 2 or attempt["ticks"] > 60 for attempt in attempts))):
        raise ValueError("prior effective budget or lifecycle differs")
    state = entry["world_lifecycle"]
    if (type(state["builds"]) is not int or state["builds"] != 1
            or state["restores"] != len(attempts)+2 or state["closed"] is not True
            or state["snapshot_released"] is not True):
        raise ValueError("prior single-world lifecycle differs")
    require_sha(state["root_digest"])


def validate_prior_search(report=None, *, root=ROOT):
    report = report if report is not None else json.loads((root/"native-prior-search-report.json").read_text())
    if (report["schema"] != "fair-final-prior-search-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["jobs"] != JOBS
            or report["horizon"] != "default-six-groups-200-ticks"
            or not report["source_sha256"] or not report["artifacts"]):
        raise ValueError("prior search suite identity differs")
    for name, key in (("run_prior_search_validation.py", "runner_sha256"), ("prior_search_worker.py", "worker_sha256")):
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("prior search runner identity changed")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((SOURCE/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("prior search source changed")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("prior search artifact changed")

    def artifact(suffix):
        paths = [root/relative for relative in report["artifacts"] if relative.endswith("/"+suffix)]
        if len(paths) != 1:
            raise ValueError("prior search artifact missing or ambiguous: "+suffix)
        return json.loads(paths[0].read_text())

    attempt, registration, preflight = artifact("attempt.json"), artifact("run-manifest.json"), artifact("preflight.json")
    if (artifact("project/probe-source-manifest.json") != report["source_sha256"]
            or [sha for name, sha in report["artifacts"].items() if name.endswith("/project/run.py")] != [report["worker_sha256"]]
            or attempt["exit_code"] != 0 or any(attempt[key] != report[key] for key in ("remote", "jobs",
                "source_sha256", "runner_sha256", "worker_sha256", "prior_file_sha", "horizon"))
            or registration["root"] != report["remote"] or registration["jobs"] != JOBS
            or preflight["memory_available"] < 64*1024**3 or preflight["free_bytes"] < 5*1024**3
            or registration["engine_sha256"] != ENGINE_SHA or preflight["engine_sha256"] != ENGINE_SHA):
        raise ValueError("prior search deployment or resource evidence differs")
    results = artifact("run-results.json")
    if ([row["job"] for row in results] != JOBS or any(row["exit_code"] != 0 or row["error"] is not None
            or row["clean"] is not True for row in results) or [shard["job"] for shard in report["shards"]] != JOBS):
        raise ValueError("prior search stage or seat coverage differs")
    prior_path = root/"public-deck-prior-engineering.json"
    prior = FrozenDeckPrior.from_payload(artifact(prior_path.name))
    if prior.to_payload() != json.loads(prior_path.read_text()) or hashlib.sha256(prior_path.read_bytes()).hexdigest() != report["prior_file_sha"]:
        raise ValueError("prior search prior file differs")
    opponent = SimpleNamespace(checkpoint_sha=SOURCE_SHA, controller_sha=semantic_digest(
        dict(source=SOURCE_SHA, adapter="fair-final-source-a", temperature=1.)))
    limits, codec, children = SearchLimits(), ActionCodec(HOG_DECK), set()
    for shard in report["shards"]:
        job, native, cleanup = shard["job"], shard["report"], shard["cleanup"]
        if (artifact(job["name"]+"/report.json") != native
                or native["schema"] != "fair-final-native-prior-search.v1" or native["status"] != "passed"
                or native["training_admissible"] is not False or native["limits"] != asdict(limits)
                or native["source_sha256"] != report["source_sha256"] or native["checkpoint_sha256"] != SOURCE_SHA
                or native["threshold"] != job["threshold"] or native["owner"] != job["owner"]
                or native["prior_sha"] != prior.sha256 or native["prior_file_sha"] != report["prior_file_sha"]
                or any(native[key] is not True for key in ("model_unchanged", "actual_root_restored", "snapshot_released"))
                or native["exclusions"] != ["population-prior-calibration", "native-local-reconditioning",
                    "normal-single-ready-root", "overtime-root", "full-episode-search-quality",
                    "training-admission", "end-to-end-throughput"]):
            raise ValueError("prior search raw report, horizon or scope differs")
        records = native["records"]
        if len(records) != 2 or [row["altered"] for row in records] != [False, True]:
            raise ValueError("prior search hidden pair missing")
        for entry in records:
            history = FairHistory.from_payload(artifact(job["name"]+f"/history-{int(entry['altered'])}.json"))
            result, audit = entry["outcome"]["result"], entry["audit"]
            manifest = audit["world_manifest"]
            factory = PriorHistoryBeliefFactory(None, prior=prior, catalog=None, opponent=opponent, sidecar_factory=None)
            provider = PriorHistorySearchProvider(factory, history=history)
            if (entry["owner"] != job["owner"] or entry["threshold"] != job["threshold"]
                    or not job["threshold"] <= entry["tick"] <= job["threshold"]+60
                    or history.root_tick != entry["tick"] or history.opening.owner != entry["owner"]
                    or history.sha256 != entry["history_sha"] or history.root_fair_sha != entry["fair_sha"]
                    or entry["actual_digest_before"] != entry["actual_digest_after"] or entry["lease_released"] is not True
                    or result["schema"] != "SearchResult/v1" or result["valid_simulations"] != 128
                    or result["unfinished_edge_backups"] != 0 or result["root_player"] != entry["owner"]
                    or result["value_perspective"] != entry["owner"] or result["root_fair_state_sha"] != history.root_fair_sha
                    or result["search_protocol_sha"] != limits.sha256 or result["noise_fraction"] != 0
                    or result["world_sampler_sha"] != provider.sampler_sha
                    or result["opponent_controller_sha"] != opponent.controller_sha
                    or type(result["invalid_attempts"]) is not int
                    or not 0 <= result["invalid_attempts"] < limits.maximum_invalid_attempts
                    or manifest["source"] != "independent-belief" or manifest["actual_state_copied"] is not False
                    or manifest["training_admissible"] is not False or manifest["prior_sha"] != prior.sha256
                    or manifest["root_sampling"] != "fresh-independent-world-per-build"
                    or manifest["scope"] != "fixed-hog-prior-search-engineering"
                    or manifest["root_fair_sha"] != history.root_fair_sha or manifest["history_sha"] != history.sha256
                    or manifest["sampler_sha"] != provider.sampler_sha
                    or manifest["opponent_controller_sha"] != opponent.controller_sha
                    or manifest["opponent_checkpoint_sha"] != SOURCE_SHA
                    or semantic_digest(manifest) != result["world_commitment"]):
                raise ValueError("prior search root, identity or world differs")
            for key in ("actual_digest_before", "actual_digest_after", "fair_sha", "history_sha"):
                require_sha(entry[key])
            if (not math.isfinite(audit["seconds"]) or audit["seconds"] <= 0
                    or set(audit["timings"]) != {"belief", "restore", "inference", "transition"}
                    or any(not math.isfinite(value) or value <= 0 for value in audit["timings"].values())
                    or sum(audit["timings"].values()) > audit["seconds"]):
                raise ValueError("prior search timing evidence differs")
            world_seed = split_seed(78102103, "world")
            history_seed = split_seed(world_seed, "conditional-history", history.sha256)
            conditioning = manifest["conditioning"]
            seeds = [split_seed(history_seed, f"attempt:{ordinal}") for ordinal in range(4)]
            accepted = [seed for seed in seeds if semantic_digest([seed]) == conditioning["seed_commitment"]]
            if len(accepted) != 1 or manifest["seed_commitment"] != semantic_digest([world_seed]):
                raise ValueError("prior search conditional seed chain differs")
            completion = HistoryCycleCursor(prior, opening=history.opening).sample(history,
                seed=split_seed(accepted[0], "opponent-draw"))
            expected = dict(source="independent-belief", actual_state_copied=False, owner=entry["owner"],
                root_tick=entry["tick"], root_fair_sha=history.root_fair_sha, history_sha=history.sha256,
                prior_sha=prior.sha256, sampler_sha=factory.sampler_sha, native_draw_verified=True,
                opponent_checkpoint_sha=SOURCE_SHA, opponent_controller_sha=opponent.controller_sha,
                scope="native-fixed-hog-prior-integration", training_admissible=False,
                completion_manifest=completion.manifest)
            if any(conditioning.get(key) != value or type(conditioning.get(key)) is not type(value) for key, value in expected.items()):
                raise ValueError("prior search conditional completion differs")
            draw, expected_draw = conditioning["native_current_draw"], completion.current_draw
            if (len(draw) != 8 or len(set(draw)) != 8 or any(type(card) is not int for card in draw)
                    or set(draw[:4]) != set(expected_draw[:4]) or tuple(draw[4:]) != expected_draw[4:]
                    or conditioning["logical_hand_order_differs"] is not (tuple(draw[:4]) != expected_draw[:4])):
                raise ValueError("prior search native hand or FIFO differs")
            candidates = result["candidates"]
            visits = {row["action_id"]:row["visits"] for row in candidates}
            if (not 1 <= len(candidates) <= 32 or len(visits) != len(candidates)
                    or any(type(count) is not int or count < 0 for count in visits.values())
                    or sum(visits.values()) != 128
                    or entry["outcome"]["target"] != [[identity, visits[identity]] for identity in sorted(visits) if visits[identity]]):
                raise ValueError("prior search visits or target differs")
            positive = sorted(identity for identity, count in visits.items() if count)
            selected = random.Random(split_seed(78102104, "behavior")).choices(positive,
                weights=[visits[identity] for identity in positive], k=1)[0]
            if codec.decode(selected).key != entry["outcome"]["selected"]:
                raise ValueError("prior search behavior sampling differs")
            mass = Counter()
            for candidate in candidates:
                action = codec.decode(candidate["action_id"])
                if (any(not math.isfinite(candidate[key]) for key in ("q", "original_joint_logp", "prior_card", "prior_projected", "prior_search"))
                        or not -1 <= candidate["q"] <= 1 or candidate["original_joint_logp"] > 1e-6
                        or not 0 <= candidate["prior_projected"] <= 1
                        or candidate["prior_projected"] != candidate["prior_search"]
                        or (action.card_id is None) != (candidate["proposal"] == "WAIT")):
                    raise ValueError("prior candidate probability or value differs")
                mass[action.card_id] += candidate["prior_projected"]
            if (None not in mass or not math.isclose(sum(mass.values()), 1., abs_tol=1e-5)
                    or not math.isclose(mass[None], result["mode_mass"][0], abs_tol=1e-5)
                    or any(not math.isclose(mass[codec.decode(row["action_id"]).card_id], row["prior_card"], abs_tol=1e-5)
                        for row in candidates)):
                raise ValueError("prior candidate mode or card mass differs")
            validate_attempts(entry, limits)
            if entry["altered"]:
                mutation = entry["mutation"]
                if mutation["before_digest"] == mutation["after_digest"] or mutation["after_digest"] != entry["actual_digest_before"]:
                    raise ValueError("prior actual hidden state was not changed")
            elif entry["mutation"] is not None:
                raise ValueError("unexpected original mutation")
        for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle"):
            if records[0][key] != records[1][key]:
                raise ValueError("hidden state changed prior search result")
        if records[0]["actual_digest_before"] != records[1]["mutation"]["before_digest"]:
            raise ValueError("prior pair did not start at same actual root")
        if (artifact(job["name"]+"/cleanup.json") != cleanup or cleanup["exit_code"] != 0
                or cleanup["error"] is not None or cleanup["clean"] is not True
                or [row["port"] for row in cleanup["engines"]] != job["ports"]):
            raise ValueError("prior engine cleanup coverage differs")
        for engine in cleanup["engines"]:
            if (engine["listening"] or engine["process"]["phase"] != "stopped"
                    or set(engine["pids_alive"]) != {"pid", "child_pid"} or any(engine["pids_alive"].values())
                    or engine["process"]["port"] != engine["port"]
                    or engine["process"]["outputs"]["libresident.so"] != ENGINE_SHA):
                raise ValueError("prior engine was not cleaned")
            children.add(engine["process"]["child_pid"])
    if len(children) != 8:
        raise ValueError("prior shards shared native engines")
    return report


if __name__ == "__main__":
    validate_prior_search()
    print(json.dumps(dict(status="passed", roots=8, effective_simulations=1024)))
