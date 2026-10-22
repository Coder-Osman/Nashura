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
from native_runner.training.fair_final.prior_history import HistoryCycleCursor
from native_runner.training.fair_final.refill_belief import verify_completion_layout
from native_runner.training.fair_final.refill_local import (
    IncrementalRefillBeliefFactory, RefillLocalSearchProvider, advance_completion, unseen_relabeling)
from native_runner.training.fair_final.search import SearchLimits, split_seed
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from tools.probe_fair_final_refill_provider import ANCHOR_SEED, SEARCH_SEED, BEHAVIOR_SEED, EXCLUSIONS
from verify_local_reconditioning import validate_phase_engine_build
from verify_prior_search import SOURCE_SHA, validate_attempts
from verify_refill import layout_from_record, validate_refill_trajectory
from verify_refill_local import duration, payload, relabeled_layout, sha


SCOPE = "anchor-conditional-unseen-permutations-not-full-posterior"


def equal_fields(value, expected, label):
    if any(value.get(key) != item or type(value.get(key)) is not type(item) for key, item in expected.items()):
        raise ValueError(label)


def validate_search(entry, history, provider, opponent):
    limits, codec = SearchLimits(), ActionCodec(HOG_DECK)
    result, audit = entry["outcome"]["result"], entry["audit"]
    manifest = audit["world_manifest"]
    equal_fields(entry, dict(owner=history.opening.owner, tick=history.root_tick,
        fair_sha=history.root_fair_sha, history_sha=history.sha256, lease_released=True,
        anchor_retained=True, anchor_restored_sha=history.root_fair_sha), "refill provider root or lease differs")
    for key in ("actual_digest_before", "actual_digest_after", "fair_sha", "history_sha"):
        require_sha(entry[key])
    if entry["actual_digest_before"] != entry["actual_digest_after"]:
        raise ValueError("refill provider changed actual match")
    equal_fields(result, dict(schema="SearchResult/v1", valid_simulations=128, unfinished_edge_backups=0,
        root_player=entry["owner"], value_perspective=entry["owner"], root_fair_state_sha=history.root_fair_sha,
        search_protocol_sha=limits.sha256, world_sampler_sha=provider.sampler_sha,
        opponent_controller_sha=opponent.controller_sha), "refill provider budget or search identity differs")
    if result["noise_fraction"] != 0 or semantic_digest(manifest) != result["world_commitment"]:
        raise ValueError("refill provider noise or world commitment differs")
    candidates = result["candidates"]
    visits = {candidate["action_id"]: candidate["visits"] for candidate in candidates}
    if (not 1 <= len(candidates) <= 32 or len(visits) != len(candidates)
            or any(type(count) is not int or count < 0 for count in visits.values())
            or sum(visits.values()) != 128
            or entry["outcome"]["target"] != [[identity, visits[identity]] for identity in sorted(visits) if visits[identity]]):
        raise ValueError("refill provider visits or policy target differs")
    positive = sorted(identity for identity in visits if visits[identity])
    selected = random.Random(split_seed(BEHAVIOR_SEED, "behavior")).choices(positive,
        weights=[visits[identity] for identity in positive], k=1)[0]
    if codec.decode(selected).key != entry["outcome"]["selected"]:
        raise ValueError("refill provider behavior sampling differs")
    masses = Counter()
    for candidate in candidates:
        action = codec.decode(candidate["action_id"])
        if (any(not math.isfinite(candidate[key]) for key in
                ("q", "original_joint_logp", "prior_card", "prior_projected", "prior_search"))
                or not -1 <= candidate["q"] <= 1 or candidate["original_joint_logp"] > 1e-6
                or not 0 <= candidate["prior_projected"] <= 1
                or candidate["prior_projected"] != candidate["prior_search"]
                or (action.card_id is None) != (candidate["proposal"] == "WAIT")):
            raise ValueError("refill provider candidate probability or value differs")
        masses[action.card_id] += candidate["prior_projected"]
    if (None not in masses or not math.isclose(sum(masses.values()), 1., abs_tol=1e-5)
            or not math.isclose(masses[None], result["mode_mass"][0], abs_tol=1e-5)
            or any(not math.isclose(masses[codec.decode(candidate["action_id"]).card_id],
                candidate["prior_card"], abs_tol=1e-5) for candidate in candidates)):
        raise ValueError("refill provider candidate mass differs")
    if (not math.isfinite(audit["seconds"]) or audit["seconds"] <= 0
            or set(audit["timings"]) != {"belief", "restore", "inference", "transition"}
            or any(not math.isfinite(value) or value <= 0 for value in audit["timings"].values())
            or sum(audit["timings"].values()) > audit["seconds"]
            or type(result["invalid_attempts"]) is not int
            or not 0 <= result["invalid_attempts"] < limits.maximum_invalid_attempts):
        raise ValueError("refill provider timing or invalid attempt evidence differs")
    lifecycle = entry["world_lifecycle"]
    if (type(lifecycle["phase_object_count"]) is not int or lifecycle["phase_object_count"] <= 0
            or lifecycle["phase_restores"] != len(entry["attempts"])+2
            or lifecycle["restores"] != len(entry["attempts"])+3):
        raise ValueError("refill provider native phase restore differs")
    prior_convention = deepcopy(entry)
    prior_convention["world_lifecycle"]["restores"] -= 1
    validate_attempts(prior_convention, limits, require_depth_coverage=False)
    duration(entry["local_profile"]["seconds"])
    if entry["local_profile"]["cycle_delta"] != dict(added_plays=0, order_checks=0):
        raise ValueError("refill provider replayed history in local draw replacement")
    if entry["altered"]:
        mutation = entry["mutation"]
        require_sha(mutation["before_digest"])
        if mutation["before_digest"] == mutation["after_digest"] or mutation["after_digest"] != entry["actual_digest_before"]:
            raise ValueError("refill provider true hidden state was not changed")
    elif entry["mutation"] is not None:
        raise ValueError("refill provider baseline unexpectedly altered")


def validate_refill_provider(report=None, *, root=ROOT):
    try:
        return validate(report, root=root)
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError("refill provider evidence incomplete or malformed") from error


def validate(report, *, root):
    from run_native_probe import BASE
    from run_refill_provider_validation import BUILD, TRACE, JOBS, payloads, source_manifest
    from run_refill_local_validation import INPUT, JOBS as HISTORY_JOBS, successor_histories

    report = report if report is not None else json.loads((root/"native-refill-provider-report.json").read_text())
    equal_fields(report, dict(schema="fair-final-refill-provider-suite.v1", status="passed",
        training_admissible=False, jobs=JOBS, purpose="refill-local-Full128-hidden-draw-intervention-real-successor"),
        "refill provider suite identity differs")
    if [shard["job"] for shard in report["shards"]] != JOBS or not report["artifacts"]:
        raise ValueError("refill provider shard or artifact coverage differs")
    for name, key in (("run_refill_provider_validation.py", "runner_sha256"), ("refill_provider_worker.py", "worker_sha256")):
        if sha(root/name) != report[key]:
            raise ValueError("refill provider deployment code changed")
    expected_sources = source_manifest()
    if report["source_sha256"] != expected_sources:
        raise ValueError("refill provider source identity changed")
    for relative, digest in report["artifacts"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts or sha(root/relative) != digest:
            raise ValueError("refill provider artifact changed or escapes evidence")

    def path(suffix):
        matches = [root/name for name in report["artifacts"] if name.endswith("/"+suffix)]
        if len(matches) != 1:
            raise ValueError("refill provider artifact missing or ambiguous: "+suffix)
        return matches[0]

    def read(suffix):
        return json.loads(path(suffix).read_text())

    validate_refill_trajectory()
    engine_sha = validate_phase_engine_build(read("engine-build-manifest.json"))
    if sha(BUILD/"libcrprobe.so") != engine_sha:
        raise ValueError("refill provider native library changed")
    settings = dict(root=report["remote"], base=BASE, jobs=JOBS, engine_sha256=engine_sha,
        engine_path=report["remote"]+"/libcrprobe.so")
    expected_payload = {name: hashlib.sha256(content).hexdigest()
        for name, content in payloads(report["remote"], engine_sha, expected_sources).items()}
    registration, attempt, preflight = read("run-manifest.json"), read("attempt.json"), read("preflight.json")
    if (registration != settings or attempt["exit_code"] != 0
            or not report["remote"].startswith("/home/zhichun/fair-final-v1-20261019-refill-provider/native-refill-provider-")
            or any(attempt[key] != report[key] for key in ("remote", "jobs", "purpose", "source_sha256",
                "runner_sha256", "worker_sha256", "engine_sha256", "payload_sha256"))
            or report["payload_sha256"] != expected_payload or report["engine_sha256"] != engine_sha
            or preflight["engine_sha256"] != engine_sha or preflight["memory_available"] < 64*1024**3
            or preflight["free_bytes"] < 5*1024**3):
        raise ValueError("refill provider deployment, engine or resources differ")
    for name, digest in expected_payload.items():
        if name != "libcrprobe.so" and sha(path(name)) != digest:
            raise ValueError("refill provider uploaded payload changed")
    prior = FrozenDeckPrior.from_payload(read("project/public-deck-prior-engineering.json"))
    opponent = SimpleNamespace(checkpoint_sha=SOURCE_SHA, controller_sha=semantic_digest(
        dict(source=SOURCE_SHA, adapter="fair-final-source-a", temperature=1.)))
    trace, successors = json.loads(TRACE.read_text()), successor_histories()
    children, all_attempts = set(), []
    if read("run-results.json") != [shard["cleanup"] for shard in report["shards"]]:
        raise ValueError("refill provider worker receipts differ")
    for shard in report["shards"]:
        job, native, cleanup = shard["job"], shard["report"], shard["cleanup"]
        prefix = job["name"]+"/"
        if read(prefix+"report.json") != native or read(prefix+"cleanup.json") != cleanup:
            raise ValueError("refill provider raw report differs")
        equal_fields(native, dict(schema="fair-final-refill-provider-native.v1", status="passed",
            training_admissible=False, source_sha256=expected_sources, checkpoint_sha256=SOURCE_SHA,
            prior_sha=prior.sha256, input_trace_sha256=sha(TRACE), tick=job["tick"], owner=job["owner"],
            limits=asdict(SearchLimits()), model_unchanged=True, actual_root_restored=True, snapshot_released=True,
            provider_closed=True, anchor_snapshot_released=True, exclusions=EXCLUSIONS), "refill provider native identity differs")
        duration(native["seconds"])
        records = native["records"]
        if [(row["stage"], row["altered"]) for row in records] != [
                (stage, altered) for stage in ("initial", "successor") for altered in (False, True)]:
            raise ValueError("refill provider paired roots or successor missing")
        historical_job = next(item for item in HISTORY_JOBS if item["tick"] == job["tick"] and item["owner"] == job["owner"])
        expected_histories = [json.loads((INPUT/historical_job["history"]).read_text()),
            json.loads(successors[historical_job["next_history"]])]
        histories = [FairHistory.from_payload(value) for value in expected_histories]
        if not histories[1].extends(histories[0]):
            raise ValueError("refill provider successor history broke its prefix")
        expected_replay = [dict(tick=group["tick"], fair_sha=group["fair_sha"])
            for group in trace["boundaries"] if group["tick"] < histories[1].root_tick]
        if (native["replayed"] != expected_replay
                or read(prefix+"progress.json") != dict(records=records, replayed=expected_replay)):
            raise ValueError("refill provider actual trajectory replay differs")
        factory = IncrementalRefillBeliefFactory(None, prior=prior, catalog=None,
            opponent=opponent, sidecar_factory=None)
        provider = RefillLocalSearchProvider(factory, history=histories[0], anchor_seed=ANCHOR_SEED)
        try:
            anchor = records[0]["audit"]["world_manifest"]["anchor"]
            conditional_seed = split_seed(ANCHOR_SEED, "conditional-history", histories[0].sha256)
            accepted = [split_seed(conditional_seed, f"attempt:{ordinal}") for ordinal in range(4)
                if semantic_digest([split_seed(conditional_seed, f"attempt:{ordinal}")]) == anchor["seed_commitment"]]
            if len(accepted) != 1:
                raise ValueError("refill provider anchor seed chain differs")
            completion = HistoryCycleCursor(prior, opening=histories[0].opening).sample(histories[0],
                seed=split_seed(accepted[0], "opponent-draw"))
            events = []
            for index, row in enumerate(records):
                history = histories[index//2]
                if read(prefix+f"history-{row['stage']}-{int(row['altered'])}.json") != expected_histories[index//2]:
                    raise ValueError("refill provider FAIR history differs from accepted trajectory")
                if index == 2:
                    completion = advance_completion(completion,
                        history.deployments[len(histories[0].deployments):], owner=1-job["owner"])
                if row["anchor_completion"] != payload(asdict(completion)):
                    raise ValueError("refill provider independent anchor completion differs")
                validate_search(row, history, provider, opponent)
                all_attempts.extend(row["attempts"])
                mapping, changed = unseen_relabeling(completion, history, seed=split_seed(SEARCH_SEED, "world"))
                layouts = [layout_from_record(record) for record in row["anchor_layouts"]]
                if len(layouts) != 2:
                    raise ValueError("refill provider anchor player missing")
                verify_completion_layout(layouts[1-job["owner"]], completion, history)
                slots = {card: slot for card, slot in zip(
                    (*layouts[1-job["owner"]].hand, *layouts[1-job["owner"]].cycle),
                    (*layouts[1-job["owner"]].hand_slots, *layouts[1-job["owner"]].cycle_slots)) if card is not None}
                expected_layouts = [record if seat == job["owner"] else relabeled_layout(record, mapping, slots)
                    for seat, record in enumerate(row["anchor_layouts"])]
                if row["local_layouts"] != expected_layouts:
                    raise ValueError("refill provider local slots, holes, FIFO or own cards differ")
                verify_completion_layout(layout_from_record(row["local_layouts"][1-job["owner"]]), changed, history)
                manifest = row["audit"]["world_manifest"]
                equal_fields(manifest["conditioning"], dict(schema=provider.conditioner.schema,
                    source="independent-belief", actual_state_copied=False, owner=job["owner"],
                    root_tick=history.root_tick, root_fair_sha=history.root_fair_sha, history_sha=history.sha256,
                    sampler_sha=provider.conditioner.sampler_sha, prior_sha=prior.sha256,
                    native_draw_verified=True, public_and_own_fair_unchanged=True, controller_unchanged=True,
                    history_ticks_replayed=0, scope=SCOPE, training_admissible=False,
                    completion_manifest=payload(changed.manifest),
                    native_current_draw=[*expected_layouts[1-job["owner"]]["hand"], *expected_layouts[1-job["owner"]]["cycle"]],
                    refill_holes=list(layouts[1-job["owner"]].holes)), "refill provider local conditional evidence differs")
                expected_manifest = dict(manifest["conditioning"], sampler_sha=provider.sampler_sha,
                    conditioning=manifest["conditioning"], anchor=manifest["anchor"], anchor_history_sha=history.sha256,
                    root_sampling="fresh-draw-on-independent-incremental-anchor")
                if manifest != expected_manifest:
                    raise ValueError("refill provider world binding differs")
                equal_fields(manifest["anchor"], dict(source="independent-belief", actual_state_copied=False,
                    owner=job["owner"], root_tick=history.root_tick, root_fair_sha=history.root_fair_sha,
                    history_sha=history.sha256, sampler_sha=factory.sampler_sha, prior_sha=prior.sha256,
                    opponent_checkpoint_sha=SOURCE_SHA, opponent_controller_sha=opponent.controller_sha,
                    native_draw_verified=True, training_admissible=False, completion_manifest=payload(completion.manifest),
                    native_current_draw=[*layouts[1-job["owner"]].hand, *layouts[1-job["owner"]].cycle],
                    refill_holes=list(layouts[1-job["owner"]].holes)), "refill provider incremental anchor metadata differs")
                event = row["local_profile"]["anchor_event"]
                previous = histories[0].opening.tick if index == 0 else histories[0 if index < 3 else 1].root_tick
                equal_fields(event, dict(mode="cold" if index == 0 else "incremental", status="verified",
                    from_tick=previous, to_tick=history.root_tick, reason=None), "refill provider cursor delta differs")
                duration(event["seconds"])
                events.append(event)
                profile = row["anchor_profile"]
                equal_fields(profile, dict(cold_ticks=histories[0].root_tick-histories[0].opening.tick,
                    incremental_ticks=0 if index < 2 else histories[1].root_tick-histories[0].root_tick,
                    ticks=history.root_tick-history.opening.tick, syncs=index+1), "refill provider historical replay budget differs")
                last = profile["last_sync"]
                equal_fields(last, dict(from_tick=previous, to_tick=history.root_tick,
                    ticks=history.root_tick-previous), "refill provider last sync differs")
                duration(last["seconds"])
            if native["cursor_events"] != events:
                raise ValueError("refill provider cursor trace differs")
            for pair in (records[:2], records[2:]):
                for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle",
                        "anchor_completion", "anchor_layouts", "local_layouts"):
                    if pair[0][key] != pair[1][key]:
                        raise ValueError("true hidden state changed refill provider "+key)
                if pair[0]["actual_digest_before"] != pair[1]["mutation"]["before_digest"]:
                    raise ValueError("refill provider true-hidden baseline differs")
        finally:
            provider.close()
            factory.close()
        equal_fields(cleanup, dict(job=job, exit_code=0, error=None, clean=True), "refill provider engine cleanup differs")
        if [engine["port"] for engine in cleanup["engines"]] != job["ports"]:
            raise ValueError("refill provider engine coverage differs")
        for engine in cleanup["engines"]:
            process, port = engine["process"], engine["port"]
            identity = process["phase_snapshot_engine"]
            if (engine["listening"] is not False or engine["pids_alive"] != dict(pid=False, child_pid=False)
                    or process["phase"] != "stopped" or process["port"] != port
                    or process != read(prefix+f"engine-{port}/process.json")
                    or identity["sha256"] != engine_sha or identity["path"] != settings["engine_path"]
                    or identity["launcher_sha256"] != expected_payload["project/phase_snapshot_launcher.py"]
                    or process["argv"].count(settings["engine_path"]) != 1
                    or type(process["child_pid"]) is not int or process["child_pid"] in children):
                raise ValueError("refill provider engine identity or release differs")
            require_sha(identity["service_sha256"])
            children.add(process["child_pid"])
    if len(children) != 12 or not any(attempt["decisions"] > 2 or attempt["ticks"] > 60 for attempt in all_attempts):
        raise ValueError("refill provider engine independence or suite depth coverage missing")
    return report


if __name__ == "__main__":
    result = validate_refill_provider()
    print(json.dumps(dict(status="passed", shards=len(result["shards"]), searches=24,
        effective_simulations=3072, training_admissible=False)))
