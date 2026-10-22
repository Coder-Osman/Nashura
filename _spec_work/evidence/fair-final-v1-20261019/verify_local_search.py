from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2]/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.local_reconditioning import SettledLocalSearchProvider
from native_runner.training.fair_final.prior_history import HistoryCycleCursor, PriorHistoryBeliefFactory
from native_runner.training.fair_final.search import SearchLimits, split_seed
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from tools.probe_fair_final_local_search import ANCHOR_SEED, SEARCH_SEED, BEHAVIOR_SEED, EXCLUSIONS
from verify_local_reconditioning import validate_phase_engine_build
from verify_prior_search import SOURCE_SHA, validate_attempts
from run_local_search_validation import JOBS


def equal_fields(value, expected, label):
    if any(value.get(key) != item or type(value.get(key)) is not type(item) for key, item in expected.items()):
        raise ValueError(label)


def validate_record(entry, history, provider, prior, opponent):
    limits, codec = SearchLimits(), ActionCodec(HOG_DECK)
    result, audit = entry["outcome"]["result"], entry["audit"]
    manifest = audit["world_manifest"]
    equal_fields(entry, dict(tick=history.root_tick, owner=history.opening.owner,
        fair_sha=history.root_fair_sha, history_sha=history.sha256, lease_released=True,
        anchor_retained=True, anchor_restored_sha=history.root_fair_sha), "local root or lease differs")
    for key in ("actual_digest_before", "actual_digest_after", "fair_sha", "history_sha"):
        require_sha(entry[key])
    if entry["actual_digest_before"] != entry["actual_digest_after"]:
        raise ValueError("search mutated actual state")
    equal_fields(result, dict(schema="SearchResult/v1", valid_simulations=128, unfinished_edge_backups=0,
        root_player=entry["owner"], value_perspective=entry["owner"], root_fair_state_sha=history.root_fair_sha,
        search_protocol_sha=limits.sha256, world_sampler_sha=provider.sampler_sha,
        opponent_controller_sha=opponent.controller_sha), "local Full128 budget or model identity differs")
    if result["noise_fraction"] != 0 or semantic_digest(manifest) != result["world_commitment"]:
        raise ValueError("local noise or manifest commitment differs")
    world_seed = split_seed(SEARCH_SEED, "world")
    completion = HistoryCycleCursor(prior, opening=history.opening).sample(history, seed=world_seed)
    expected = dict(schema="fair-final-settled-local-draw.v1", source="independent-belief",
        actual_state_copied=False, owner=entry["owner"], root_tick=entry["tick"],
        root_fair_sha=history.root_fair_sha, history_sha=history.sha256, prior_sha=prior.sha256,
        sampler_sha=provider.conditioner.sampler_sha, native_draw_verified=True,
        public_and_own_fair_unchanged=True, controller_unchanged=True, history_ticks_replayed=0,
        seed_commitment=semantic_digest([world_seed]), completion_manifest=completion.manifest,
        native_current_draw=list(sorted(completion.current_draw[:4]))+list(completion.current_draw[4:]),
        scope="settled-root-local-draw-engineering", training_admissible=False,
        opponent_checkpoint_sha=SOURCE_SHA, opponent_controller_sha=opponent.controller_sha)
    equal_fields(manifest["conditioning"], expected, "local conditional draw evidence differs")
    equal_fields(manifest, dict(expected, sampler_sha=provider.sampler_sha,
        root_sampling="fresh-draw-on-independent-incremental-anchor", anchor_history_sha=history.sha256),
        "local provider manifest differs")
    require_sha(manifest["conditioning"]["opponent_fair_sha"])
    if manifest["opponent_fair_sha"] != manifest["conditioning"]["opponent_fair_sha"]:
        raise ValueError("conditioned opponent view differs")
    candidates = result["candidates"]
    visits = {candidate["action_id"]:candidate["visits"] for candidate in candidates}
    if (not 1 <= len(candidates) <= 32 or len(visits) != len(candidates)
            or any(type(count) is not int or count < 0 for count in visits.values())
            or sum(visits.values()) != 128
            or entry["outcome"]["target"] != [[identity, visits[identity]] for identity in sorted(visits) if visits[identity]]):
        raise ValueError("local visit counts or target differ")
    positive = sorted(identity for identity in visits if visits[identity])
    selected = random.Random(split_seed(BEHAVIOR_SEED, "behavior")).choices(positive,
        weights=[visits[identity] for identity in positive], k=1)[0]
    if codec.decode(selected).key != entry["outcome"]["selected"]:
        raise ValueError("local behavior sampling differs")
    masses = Counter()
    for candidate in candidates:
        action = codec.decode(candidate["action_id"])
        if (any(not math.isfinite(candidate[key]) for key in
                ("q", "original_joint_logp", "prior_card", "prior_projected", "prior_search"))
                or not -1 <= candidate["q"] <= 1 or candidate["original_joint_logp"] > 1e-6
                or not 0 <= candidate["prior_projected"] <= 1
                or candidate["prior_projected"] != candidate["prior_search"]
                or (action.card_id is None) != (candidate["proposal"] == "WAIT")):
            raise ValueError("local candidate probabilities differ")
        masses[action.card_id] += candidate["prior_projected"]
    if (None not in masses or not math.isclose(sum(masses.values()), 1., abs_tol=1e-5)
            or not math.isclose(masses[None], result["mode_mass"][0], abs_tol=1e-5)
            or any(not math.isclose(masses[codec.decode(candidate["action_id"]).card_id],
                candidate["prior_card"], abs_tol=1e-5) for candidate in candidates)):
        raise ValueError("local candidate probability mass differs")
    if (not math.isfinite(audit["seconds"]) or audit["seconds"] <= 0
            or set(audit["timings"]) != {"belief", "restore", "inference", "transition"}
            or any(not math.isfinite(value) or value <= 0 for value in audit["timings"].values())
            or sum(audit["timings"].values()) > audit["seconds"]
            or type(result["invalid_attempts"]) is not int
            or not 0 <= result["invalid_attempts"] < limits.maximum_invalid_attempts):
        raise ValueError("local timing or invalid attempt evidence differs")
    state = entry["world_lifecycle"]
    if (type(state["phase_object_count"]) is not int or state["phase_object_count"] <= 0
            or state["phase_restores"] != len(entry["attempts"])+2
            or state["restores"] != len(entry["attempts"])+3):
        raise ValueError("local phase restore coverage differs")
    prior_convention = deepcopy(entry)
    prior_convention["world_lifecycle"]["restores"] -= 1
    validate_attempts(prior_convention, limits)
    profile = entry["local_profile"]
    if not math.isfinite(profile["seconds"]) or profile["seconds"] <= 0:
        raise ValueError("local reconditioning timing differs")
    event = profile["anchor_event"]
    equal_fields(event, dict(status="verified", to_tick=history.root_tick), "local anchor event differs")
    if event["mode"] not in ("cold", "incremental", "rebuild"):
        raise ValueError("unknown local anchor mode")
    if entry["altered"]:
        mutation = entry["mutation"]
        if mutation["before_digest"] == mutation["after_digest"] or mutation["after_digest"] != entry["actual_digest_before"]:
            raise ValueError("true hidden state was not changed")
    elif entry["mutation"] is not None:
        raise ValueError("unexpected baseline mutation")


def validate_local_search(report=None, *, root=ROOT):
    if report is None:
        report = json.loads((root/"native-local-search-report.json").read_text())
    equal_fields(report, dict(schema="fair-final-local-search-suite.v1", status="passed",
        training_admissible=False, jobs=JOBS, horizon="default-six-groups-200-ticks"), "local suite identity differs")
    for name, key in (("run_local_search_validation.py", "runner_sha256"), ("local_search_worker.py", "worker_sha256")):
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("local suite runner changed")
    if not report["source_sha256"] or not report["artifacts"]:
        raise ValueError("local source or artifact manifest missing")
    for relative, expected in report["source_sha256"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("source path escapes project")
        if hashlib.sha256((SOURCE/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("local suite source changed: "+relative)
    if "native_runner/training_capture.py" not in report["source_sha256"]:
        raise ValueError("decoder source missing")
    for relative, expected in report["artifacts"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("local artifact path escapes root")
        if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("local artifact changed: "+relative)

    def artifact_path(suffix):
        paths = [root/relative for relative in report["artifacts"] if relative.endswith("/"+suffix)]
        if len(paths) != 1:
            raise ValueError("local artifact missing or ambiguous: "+suffix)
        return paths[0]

    def artifact(suffix):
        return json.loads(artifact_path(suffix).read_text())

    engine_sha = validate_phase_engine_build(artifact("engine-build-manifest.json"))
    registration, attempt, preflight = artifact("run-manifest.json"), artifact("attempt.json"), artifact("preflight.json")
    if (report["engine_sha256"] != engine_sha or registration["engine_sha256"] != engine_sha
            or preflight["engine_sha256"] != engine_sha or registration["root"] != report["remote"]
            or registration["jobs"] != JOBS or registration["engine_path"] != report["remote"]+"/libcrprobe.so"
            or preflight["memory_available"] < 64*1024**3 or preflight["free_bytes"] < 5*1024**3
            or artifact("project/probe-source-manifest.json") != report["source_sha256"]
            or attempt["exit_code"] != 0 or any(attempt[key] != report[key] for key in
                ("remote", "jobs", "source_sha256", "runner_sha256", "worker_sha256", "prior_file_sha", "horizon"))
            or hashlib.sha256(artifact_path("project/run.py").read_bytes()).hexdigest() != report["worker_sha256"]):
        raise ValueError("local deployment or resource evidence differs")
    launcher_sha = hashlib.sha256((root/"phase_snapshot_launcher.py").read_bytes()).hexdigest()
    if hashlib.sha256(artifact_path("project/phase_snapshot_launcher.py").read_bytes()).hexdigest() != launcher_sha:
        raise ValueError("local engine launcher differs")
    prior_path = root/"public-deck-prior-engineering.json"
    prior = FrozenDeckPrior.from_payload(artifact(prior_path.name))
    if (prior.to_payload() != json.loads(prior_path.read_text())
            or hashlib.sha256(prior_path.read_bytes()).hexdigest() != report["prior_file_sha"]):
        raise ValueError("local prior file differs")
    results = artifact("run-results.json")
    if ([row["job"] for row in results] != JOBS or [shard["job"] for shard in report["shards"]] != JOBS
            or any(row["exit_code"] != 0 or row["error"] is not None or row["clean"] is not True for row in results)):
        raise ValueError("local shard coverage or cleanup differs")
    opponent = SimpleNamespace(checkpoint_sha=SOURCE_SHA, controller_sha=semantic_digest(
        dict(source=SOURCE_SHA, adapter="fair-final-source-a", temperature=1.)))
    children = set()
    for shard in report["shards"]:
        job, native, cleanup = shard["job"], shard["report"], shard["cleanup"]
        if artifact(job["name"]+"/report.json") != native:
            raise ValueError("local raw report differs")
        equal_fields(native, dict(schema="fair-final-native-local-search.v1", status="passed",
            training_admissible=False, source_sha256=report["source_sha256"], checkpoint_sha256=SOURCE_SHA,
            prior_sha=prior.sha256, prior_file_sha=report["prior_file_sha"], threshold=job["threshold"], owner=job["owner"],
            limits=asdict(SearchLimits()), model_unchanged=True, actual_root_restored=True, snapshot_released=True,
            provider_closed=True, anchor_snapshot_released=True, exclusions=EXCLUSIONS), "local native scope or identity differs")
        records = native["records"]
        if [(entry["stage"], entry["altered"]) for entry in records] != [
                (stage, altered) for stage in ("initial", "successor") for altered in (False, True)]:
            raise ValueError("local hidden pair or successor missing")
        histories = []
        for entry in records:
            history = FairHistory.from_payload(artifact(job["name"]+f"/history-{entry['stage']}-{int(entry['altered'])}.json"))
            histories.append(history)
            factory = PriorHistoryBeliefFactory(None, prior=prior, catalog=None, opponent=opponent, sidecar_factory=None)
            provider = SettledLocalSearchProvider(factory, history=history, anchor_seed=ANCHOR_SEED)
            if entry["owner"] != job["owner"] or entry["threshold"] != job["threshold"]:
                raise ValueError("local stage or seat differs")
            validate_record(entry, history, provider, prior, opponent)
            provider.close()
            factory.close()
        if (not job["threshold"] <= histories[0].root_tick <= job["threshold"]+60
                or not histories[2].extends(histories[0])
                or not histories[0].root_tick < histories[2].root_tick <= histories[0].root_tick+60):
            raise ValueError("local successor prefix or tick coverage differs")
        for pair in (records[:2], records[2:]):
            for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle"):
                if pair[0][key] != pair[1][key]:
                    raise ValueError("actual hidden state changed search: "+key)
            if pair[0]["actual_digest_before"] != pair[1]["mutation"]["before_digest"]:
                raise ValueError("actual hidden pair baseline differs")
        events = [entry["local_profile"]["anchor_event"] for entry in records]
        if (native["cursor_events"] != events or [event["mode"] for event in events] !=
                ["cold", "incremental", "incremental", "incremental"]
                or any(event["reason"] is not None for event in events)):
            raise ValueError("local incremental coverage requires no cold successor rebuild")
        if (events[0]["from_tick"] != histories[0].opening.tick
                or events[1]["from_tick"] != histories[0].root_tick
                or events[2]["from_tick"] != histories[0].root_tick
                or events[3]["from_tick"] != histories[2].root_tick):
            raise ValueError("local cursor delta differs")
        cold_ticks = histories[0].root_tick-histories[0].opening.tick
        anchor = records[0]["audit"]["world_manifest"]["anchor"]
        initial = histories[0]
        conditional_seed = split_seed(ANCHOR_SEED, "conditional-history", initial.sha256)
        accepted = [split_seed(conditional_seed, f"attempt:{ordinal}") for ordinal in range(4)
            if semantic_digest([split_seed(conditional_seed, f"attempt:{ordinal}")]) == anchor["seed_commitment"]]
        if len(accepted) != 1:
            raise ValueError("local independent anchor seed chain differs")
        completion = HistoryCycleCursor(prior, opening=initial.opening).sample(initial,
            seed=split_seed(accepted[0], "opponent-draw"))
        equal_fields(anchor, dict(source="independent-belief", actual_state_copied=False,
            owner=initial.opening.owner, root_tick=initial.root_tick, root_fair_sha=initial.root_fair_sha,
            history_sha=initial.sha256, prior_sha=prior.sha256, sampler_sha=factory.sampler_sha,
            native_draw_verified=True, opponent_checkpoint_sha=SOURCE_SHA,
            opponent_controller_sha=opponent.controller_sha, scope="native-fixed-hog-prior-integration",
            training_admissible=False, completion_manifest=completion.manifest), "local anchor construction differs")
        draw = anchor["native_current_draw"]
        if (len(draw) != 8 or set(draw[:4]) != set(completion.current_draw[:4])
                or tuple(draw[4:]) != completion.current_draw[4:]
                or anchor["logical_hand_order_differs"] is not (tuple(draw[:4]) != completion.current_draw[:4])):
            raise ValueError("local native anchor draw differs")
        for index, entry in enumerate(records):
            profile = entry["anchor_profile"]
            expected_delta = 0 if index < 2 else histories[2].root_tick-histories[0].root_tick
            if profile["cold_ticks"] != cold_ticks or profile["incremental_ticks"] != expected_delta:
                raise ValueError("local anchor replay tick budget differs")
            if entry["audit"]["world_manifest"]["anchor"] != records[0]["audit"]["world_manifest"]["anchor"]:
                raise ValueError("local persistent anchor construction identity changed")
        if (artifact(job["name"]+"/cleanup.json") != cleanup or cleanup["exit_code"] != 0
                or cleanup["error"] is not None or cleanup["clean"] is not True
                or [engine["port"] for engine in cleanup["engines"]] != job["ports"]):
            raise ValueError("local cleanup receipt differs")
        for engine in cleanup["engines"]:
            process = engine["process"]
            identity = process["phase_snapshot_engine"]
            if (engine["listening"] or any(engine["pids_alive"].values())
                    or set(engine["pids_alive"]) != {"pid", "child_pid"} or process["phase"] != "stopped"
                    or process["port"] != engine["port"] or identity["sha256"] != engine_sha
                    or identity["path"] != registration["engine_path"] or identity["launcher_sha256"] != launcher_sha):
                raise ValueError("local engine identity or cleanup differs")
            require_sha(identity["service_sha256"])
            if artifact(job["name"]+f"/engine-{engine['port']}/process.json") != process:
                raise ValueError("local engine process evidence differs")
            children.add(process["child_pid"])
    if len(children) != 8:
        raise ValueError("local shards shared engines")
    return report


if __name__ == "__main__":
    validate_local_search()
    print(json.dumps(dict(status="passed", roots=16, effective_simulations=2048)))
