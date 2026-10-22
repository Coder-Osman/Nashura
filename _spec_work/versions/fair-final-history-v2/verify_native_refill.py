import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"
PROJECT = ROOT / "project"
sys.path.insert(0, str(PROJECT))

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.prior_history import HistoryCycleCursor
from native_runner.training.fair_final.refill_belief import verify_completion_layout
from native_runner.training.fair_final.refill_local import (
    IncrementalRefillBeliefFactory, RefillLocalSearchProvider, advance_completion, unseen_relabeling)
from native_runner.training.fair_final.search import SearchLimits, split_seed
from native_runner.training.fair_search.contracts import semantic_digest
from tools.probe_fair_final_refill_provider import ANCHOR_SEED, SEARCH_SEED, EXCLUSIONS

sys.path.insert(0, str(EVIDENCE))

from verify_refill_provider import validate_search, equal_fields, SCOPE, SOURCE_SHA
from verify_refill import layout_from_record
from verify_refill_local import relabeled_layout, payload, duration
from verify_local_reconditioning import validate_phase_engine_build
from run_native_refill import JOBS, TRACE, source_manifest, payloads, sha, BASE


def validate(attempt_directory):
    directory = attempt_directory.resolve()
    if directory.parent != ROOT or not directory.name.startswith("native-history-v2-refill-"):
        raise ValueError("native attempt escapes isolated version")

    def read(name):
        return json.loads((directory / name).read_text())

    attempt = read("attempt.json")
    equal_fields(attempt, dict(schema="fair-final-history-v2-native-attempt.v1", jobs=JOBS,
        training_admissible=False, exit_code=0,
        purpose="history-v2-bounded-local-Full128-hidden-pairs-and-incremental-replay"), "native attempt failed or changed scope")
    engine_sha = validate_phase_engine_build(read("engine-build-manifest.json"))
    if attempt["engine_sha256"] != engine_sha:
        raise ValueError("native engine differs from frozen build")
    if (attempt["version_manifest_sha256"] != sha(ROOT / "version-manifest.json")
            or attempt["runner_sha256"] != sha(ROOT / "run_native_refill.py")
            or attempt["worker_sha256"] != sha(EVIDENCE / "refill_provider_worker.py")
            or attempt["source_sha256"] != source_manifest()
            or attempt["remote"] != "/home/zhichun/fair-final-history-v2/" + directory.name):
        raise ValueError("version or deployment identity changed")
    settings = read("run-manifest.json")
    expected_settings = dict(root=attempt["remote"], base=BASE, jobs=JOBS,
        engine_sha256=attempt["engine_sha256"], engine_path=attempt["remote"] + "/libcrprobe.so")
    if settings != expected_settings:
        raise ValueError("native worker registration changed")
    expected_payloads = payloads(attempt["remote"], attempt["engine_sha256"], attempt["source_sha256"])
    if attempt["payload_sha256"] != {name: hashlib.sha256(content).hexdigest()
            for name, content in expected_payloads.items()}:
        raise ValueError("native payload identity changed")
    for name, content in expected_payloads.items():
        if name != "libcrprobe.so" and (directory / name).read_bytes() != content:
            raise ValueError("collected native payload changed")
    preflight = read("preflight.json")
    if (preflight["engine_sha256"] != attempt["engine_sha256"]
            or preflight["memory_available"] < 64 * 1024**3
            or preflight["free_bytes"] < 5 * 1024**3
            or preflight["formal_storage_gate_passed"] is not False):
        raise ValueError("native diagnostic resource evidence differs")
    results = read("run-results.json")
    if len(results) != 1:
        raise ValueError("unexpected native job count")
    job = JOBS[0]
    prefix = job["name"] + "/"
    cleanup = read(prefix + "cleanup.json")
    equal_fields(cleanup, dict(job=job, exit_code=0, error=None, clean=True), "native cleanup failed")
    if results != [cleanup] or [entry["port"] for entry in cleanup["engines"]] != job["ports"]:
        raise ValueError("native cleanup receipt differs")
    children = set()
    for entry in cleanup["engines"]:
        process, port = entry["process"], entry["port"]
        identity = process["phase_snapshot_engine"]
        if (process != read(prefix + f"engine-{port}/process.json")
                or process["phase"] != "stopped" or process["port"] != port
                or entry["listening"] is not False
                or entry["pids_alive"] != dict(pid=False, child_pid=False)
                or identity["sha256"] != attempt["engine_sha256"]
                or identity["path"] != settings["engine_path"]
                or identity["launcher_sha256"] != attempt["payload_sha256"]["project/phase_snapshot_launcher.py"]
                or type(process["child_pid"]) is not int):
            raise ValueError("native engine identity or release differs")
        children.add(process["child_pid"])
    if len(children) != 2:
        raise ValueError("native independent engines missing")
    native = read(prefix + "report.json")
    prior = FrozenDeckPrior.from_payload(read("project/public-deck-prior-engineering.json"))
    equal_fields(native, dict(schema="fair-final-refill-provider-native.v1", status="passed",
        training_admissible=False, source_sha256=attempt["source_sha256"], checkpoint_sha256=SOURCE_SHA,
        prior_sha=prior.sha256, input_trace_sha256=sha(TRACE), tick=job["tick"], owner=job["owner"],
        limits=asdict(SearchLimits()), model_unchanged=True, actual_root_restored=True, snapshot_released=True,
        provider_closed=True, anchor_snapshot_released=True, exclusions=EXCLUSIONS), "native identity or scope differs")
    duration(native["seconds"])
    records = native["records"]
    if [(row["stage"], row["altered"]) for row in records] != [
            (stage, altered) for stage in ("initial", "successor") for altered in (False, True)]:
        raise ValueError("native paired roots missing")
    histories = [FairHistory.from_payload(read(prefix + f"history-{stage}-0.json"))
        for stage in ("initial", "successor")]
    if not histories[1].extends(histories[0]) or any(history.rejections for history in histories):
        raise ValueError("native local history scope changed")
    trace = json.loads(TRACE.read_text())
    archived = json.loads((TRACE.parent.parent / "trajectory/history-110-0.json").read_text())
    if any(payload(asdict(history.opening)) != archived["opening"] for history in histories):
        raise ValueError("native opening evidence differs from frozen trajectory")
    successor = next(group["tick"] for group in trace["boundaries"] if group["tick"] > job["tick"])
    if [history.root_tick for history in histories] != [job["tick"], successor]:
        raise ValueError("native trajectory roots changed")
    for history in histories:
        expected_decisions = [dict(tick=group["tick"], fair_sha=group["fair_sha"][str(job["owner"])],
            action=group["actions"][str(job["owner"])] ) for group in trace["boundaries"]
            if group["tick"] < history.root_tick and str(job["owner"]) in group["fair_sha"]]
        expected_deployments = sorted([dict(tick=event["tick"], owner=event["owner"], card_id=event["card_id"],
            x=int(event["position"][0]), y=int(event["position"][1]))
            for tick in trace["ticks"] if history.opening.tick < tick["tick"] <= history.root_tick
            for event in tick["public_executions"]], key=lambda event: (event["tick"], event["owner"]))
        group = next(group for group in trace["boundaries"] if group["tick"] == history.root_tick)
        if (payload([asdict(decision) for decision in history.decisions]) != expected_decisions
                or payload([asdict(event) for event in history.deployments]) != expected_deployments
                or history.root_fair_sha != group["fair_sha"][str(job["owner"])]):
            raise ValueError("native FAIR history differs from frozen public trajectory")
    expected_replay = [dict(tick=group["tick"], fair_sha=group["fair_sha"])
        for group in trace["boundaries"] if group["tick"] < successor]
    if (native["replayed"] != expected_replay
            or read(prefix + "progress.json") != dict(records=records, replayed=expected_replay)):
        raise ValueError("native trajectory replay differs")
    opponent = SimpleNamespace(checkpoint_sha=SOURCE_SHA, controller_sha=semantic_digest(
        dict(source=SOURCE_SHA, adapter="fair-final-source-a", temperature=1.)))
    factory = IncrementalRefillBeliefFactory(None, prior=prior, catalog=None, opponent=opponent, sidecar_factory=None)
    provider = RefillLocalSearchProvider(factory, history=histories[0], anchor_seed=ANCHOR_SEED)
    try:
        anchor = records[0]["audit"]["world_manifest"]["anchor"]
        conditional_seed = split_seed(ANCHOR_SEED, "conditional-history", histories[0].sha256)
        accepted = [split_seed(conditional_seed, f"attempt:{ordinal}") for ordinal in range(4)
            if semantic_digest([split_seed(conditional_seed, f"attempt:{ordinal}")]) == anchor["seed_commitment"]]
        if len(accepted) != 1:
            raise ValueError("native independent seed chain differs")
        completion = HistoryCycleCursor(prior, opening=histories[0].opening).sample(histories[0],
            seed=split_seed(accepted[0], "opponent-draw"))
        events = []
        for index, row in enumerate(records):
            history = histories[index // 2]
            if read(prefix + f"history-{row['stage']}-{int(row['altered'])}.json") != payload(history.to_payload()):
                raise ValueError("hidden mutation changed available history")
            if index == 2:
                completion = advance_completion(completion,
                    history.deployments[len(histories[0].deployments):], owner=1 - job["owner"])
            if row["anchor_completion"] != payload(asdict(completion)):
                raise ValueError("native independent anchor completion differs")
            validate_search(row, history, provider, opponent)
            mapping, changed = unseen_relabeling(completion, history, seed=split_seed(SEARCH_SEED, "world"))
            layouts = [layout_from_record(record) for record in row["anchor_layouts"]]
            if len(layouts) != 2:
                raise ValueError("native player layout missing")
            hidden = layouts[1 - job["owner"]]
            verify_completion_layout(hidden, completion, history)
            slots = {card: slot for card, slot in zip((*hidden.hand, *hidden.cycle),
                (*hidden.hand_slots, *hidden.cycle_slots)) if card is not None}
            expected_layouts = [record if seat == job["owner"] else relabeled_layout(record, mapping, slots)
                for seat, record in enumerate(row["anchor_layouts"])]
            if row["local_layouts"] != expected_layouts:
                raise ValueError("native local draw or own cards differ")
            verify_completion_layout(layout_from_record(row["local_layouts"][1 - job["owner"]]), changed, history)
            manifest = row["audit"]["world_manifest"]
            equal_fields(manifest["conditioning"], dict(schema=provider.conditioner.schema,
                source="independent-belief", actual_state_copied=False, owner=job["owner"],
                root_tick=history.root_tick, root_fair_sha=history.root_fair_sha, history_sha=history.sha256,
                sampler_sha=provider.conditioner.sampler_sha, prior_sha=prior.sha256,
                native_draw_verified=True, public_and_own_fair_unchanged=True, controller_unchanged=True,
                history_ticks_replayed=0, scope=SCOPE, training_admissible=False,
                completion_manifest=payload(changed.manifest),
                native_current_draw=[*expected_layouts[1 - job["owner"]]["hand"],
                    *expected_layouts[1 - job["owner"]]["cycle"]], refill_holes=list(hidden.holes)), "native conditioning differs")
            expected_manifest = dict(manifest["conditioning"], sampler_sha=provider.sampler_sha,
                conditioning=manifest["conditioning"], anchor=manifest["anchor"], anchor_history_sha=history.sha256,
                root_sampling="fresh-draw-on-independent-incremental-anchor")
            if manifest != expected_manifest:
                raise ValueError("native world commitment differs")
            equal_fields(manifest["anchor"], dict(source="independent-belief", actual_state_copied=False,
                owner=job["owner"], root_tick=history.root_tick, root_fair_sha=history.root_fair_sha,
                history_sha=history.sha256, sampler_sha=factory.sampler_sha, prior_sha=prior.sha256,
                opponent_checkpoint_sha=SOURCE_SHA, opponent_controller_sha=opponent.controller_sha,
                native_draw_verified=True, training_admissible=False, completion_manifest=payload(completion.manifest),
                native_current_draw=[*hidden.hand, *hidden.cycle], refill_holes=list(hidden.holes)), "native anchor differs")
            previous = histories[0].opening.tick if index == 0 else histories[0 if index < 3 else 1].root_tick
            event = row["local_profile"]["anchor_event"]
            equal_fields(event, dict(mode="cold" if index == 0 else "incremental", status="verified",
                from_tick=previous, to_tick=history.root_tick, reason=None), "native incremental cursor differs")
            events.append(event)
            profile = row["anchor_profile"]
            equal_fields(profile, dict(cold_ticks=histories[0].root_tick - histories[0].opening.tick,
                incremental_ticks=0 if index < 2 else histories[1].root_tick - histories[0].root_tick,
                ticks=history.root_tick - history.opening.tick, syncs=index + 1), "native replay budget differs")
            equal_fields(profile["last_sync"], dict(from_tick=previous, to_tick=history.root_tick,
                ticks=history.root_tick - previous), "native sync boundary differs")
            for value in (event["seconds"], profile["last_sync"]["seconds"]):
                duration(value)
        if native["cursor_events"] != events:
            raise ValueError("native cursor trace differs")
        for pair in (records[:2], records[2:]):
            for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle",
                    "anchor_completion", "anchor_layouts", "local_layouts"):
                if pair[0][key] != pair[1][key]:
                    raise ValueError("actual hidden draw influenced search")
            if pair[0]["actual_digest_before"] != pair[1]["mutation"]["before_digest"]:
                raise ValueError("hidden mutation baseline differs")
    finally:
        provider.close()
        factory.close()
    imports = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
        if name.startswith(("native_runner", "tools.probe_fair_final")) and getattr(module, "__file__", None)}
    if any(not Path(path).is_relative_to(PROJECT) for path in imports.values()):
        raise ValueError("validator imported a different runtime version")
    launch = json.loads((EVIDENCE / "native-search-episode-1792552718166807000/launch.json").read_text())
    if any(sha(WORKSPACE / "firstlight_CR" / name) != digest for name, digest in launch["source_sha256"].items()):
        raise ValueError("running original attempt source changed")
    return dict(schema="fair-final-history-v2-native-verification.v1", status="passed_limited_scope",
        searches=len(records), valid_simulations=sum(row["outcome"]["result"]["valid_simulations"] for row in records),
        hidden_pairs=2, independent_engines=2, engine_cleanup_passed=True,
        history_schema=FairHistory.schema, version_manifest_sha256=sha(ROOT / "version-manifest.json"),
        general_pending_closed=False, native_rejection_reachability_proven=False,
        complete_game_verified=False, training_admissible=False, exclusions=EXCLUSIONS,
        imported_runtime=imports, unchanged_original_source_files=len(launch["source_sha256"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    report = validate(args.attempt)
    report["verifier_sha256"] = sha(Path(__file__))
    report["artifacts"] = {str(path.relative_to(args.attempt)): sha(path)
        for path in sorted(args.attempt.rglob("*")) if path.is_file() and path.name != "verification.json"}
    report["helper_sha256"] = {name: sha(EVIDENCE / name) for name in (
        "verify_refill_provider.py", "verify_prior_search.py", "verify_refill.py", "verify_refill_local.py",
        "verify_local_reconditioning.py")}
    (args.attempt / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items()
        if key not in ("artifacts", "imported_runtime", "helper_sha256")}), flush=True)


if __name__ == "__main__":
    main()
