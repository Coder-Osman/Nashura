import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
HISTORY = ROOT.parent / "fair-final-history-v2"
PROJECT = HISTORY / "project"
EVIDENCE = ROOT.parents[2] / "_spec_work/evidence/fair-final-v1-20261019"
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(EVIDENCE))

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.prior_history import HistoryCycleCursor
from native_runner.training.fair_final.refill_belief import verify_completion_layout
from native_runner.training.fair_final.refill_local import (
    IncrementalRefillBeliefFactory, advance_completion, unseen_relabeling)
from native_runner.training.fair_final.search import SearchLimits, split_seed
from native_runner.training.fair_search.contracts import semantic_digest
from tools.probe_fair_final_refill_provider import ANCHOR_SEED, SEARCH_SEED, EXCLUSIONS
from tools.probe_fair_final_prior_search import SOURCE_SHA
from general_provider import GeneralHistorySearchProvider
from run_native_general import JOBS, payloads, refill_runner
from verify_local_reconditioning import validate_phase_engine_build
from verify_refill import layout_from_record
from verify_refill_local import duration, payload, relabeled_layout
from verify_refill_provider import equal_fields, validate_search


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_world(row, history, provider, opponent):
    manifest = row["audit"]["world_manifest"]
    construction = manifest["construction"]
    local = provider._local_provider()
    expected = dict(schema="fair-final-general-history-world.v1", source="independent-belief",
        actual_state_copied=False, root_fair_sha=history.root_fair_sha, history_sha=history.sha256,
        sampler_sha=provider.sampler_sha, seed_commitment=semantic_digest([split_seed(SEARCH_SEED, "world")]),
        mode="local", fallback_reason=None, construction=construction,
        scope="bounded-general-history-engineering", training_admissible=False)
    if manifest != expected:
        raise ValueError("general native world identity or local scope differs")
    if any(type(manifest[key]) is not bool or manifest[key] is not False
            for key in ("actual_state_copied", "training_admissible")):
        raise ValueError("general native boolean contract differs")
    equal_fields(construction, dict(sampler_sha=local.sampler_sha, source="independent-belief",
        actual_state_copied=False, root_fair_sha=history.root_fair_sha, history_sha=history.sha256,
        anchor_history_sha=history.sha256, root_sampling="fresh-draw-on-independent-incremental-anchor",
        training_admissible=False), "general native inner construction differs")
    equal_fields(construction["conditioning"], dict(schema=local.conditioner.schema,
        sampler_sha=local.conditioner.sampler_sha, root_fair_sha=history.root_fair_sha,
        history_sha=history.sha256, actual_state_copied=False, history_ticks_replayed=0,
        native_draw_verified=True, public_and_own_fair_unchanged=True, controller_unchanged=True,
        training_admissible=False), "general native local conditioning differs")
    equal_fields(construction["anchor"], dict(sampler_sha=provider.factory.sampler_sha,
        source="independent-belief", actual_state_copied=False, root_fair_sha=history.root_fair_sha,
        history_sha=history.sha256, native_draw_verified=True, training_admissible=False,
        opponent_checkpoint_sha=opponent.checkpoint_sha, opponent_controller_sha=opponent.controller_sha),
        "general native independent anchor differs")
    validate_search(row, history, provider, opponent)
    if row["outcome"]["result"]["world_sampler_sha"] != provider.sampler_sha:
        raise ValueError("general native search used another sampler")


def validate_layouts(row, history, completion):
    if row["anchor_completion"] != payload(asdict(completion)):
        raise ValueError("general native independent anchor completion differs")
    mapping, changed = unseen_relabeling(completion, history, seed=split_seed(SEARCH_SEED, "world"))
    layouts = [layout_from_record(record) for record in row["anchor_layouts"]]
    if len(layouts) != 2:
        raise ValueError("general native player layout missing")
    hidden = layouts[1 - history.opening.owner]
    verify_completion_layout(hidden, completion, history)
    slots = {card: slot for card, slot in zip((*hidden.hand, *hidden.cycle),
        (*hidden.hand_slots, *hidden.cycle_slots)) if card is not None}
    expected = [record if seat == history.opening.owner else relabeled_layout(record, mapping, slots)
        for seat, record in enumerate(row["anchor_layouts"])]
    if row["local_layouts"] != expected:
        raise ValueError("general native local draw or own cards differ")
    verify_completion_layout(layout_from_record(expected[1 - history.opening.owner]), changed, history)
    inner = row["audit"]["world_manifest"]["construction"]
    if (inner["conditioning"]["completion_manifest"] != payload(changed.manifest)
            or inner["anchor"]["completion_manifest"] != payload(completion.manifest)
            or inner["conditioning"]["native_current_draw"] != [*expected[1 - history.opening.owner]["hand"],
                *expected[1 - history.opening.owner]["cycle"]]
            or inner["conditioning"]["refill_holes"] != list(hidden.holes)
            or inner["anchor"]["native_current_draw"] != [*hidden.hand, *hidden.cycle]
            or inner["anchor"]["refill_holes"] != list(hidden.holes)):
        raise ValueError("general native draw manifests differ")


def validate(directory):
    directory = directory.resolve()
    if directory.parent != ROOT or not directory.name.startswith("native-general-local-"):
        raise ValueError("general native attempt escapes version root")
    def read(name):
        return json.loads((directory / name).read_text())
    attempt = read("attempt.json")
    equal_fields(attempt, dict(schema="fair-final-general-native-attempt.v1", jobs=JOBS,
        exit_code=0, training_admissible=False,
        purpose="general-provider-native-local-hidden-pairs-incremental-Full128"),
        "general native attempt failed or changed scope")
    identities = refill_runner.source_manifest()
    identities.update({name: sha(ROOT / name) for name in ("general_provider.py", "probe_native_general.py")})
    engine_sha = validate_phase_engine_build(read("engine-build-manifest.json"))
    if (attempt["source_sha256"] != identities
            or attempt["runner_sha256"] != sha(ROOT / "run_native_general.py")
            or attempt["dependency_sha256"] != sha(HISTORY / "run_native_refill.py")
            or attempt["history_version_sha256"] != sha(HISTORY / "version-manifest.json")
            or attempt["engine_sha256"] != engine_sha
            or attempt["remote"] != "/home/zhichun/fair-final-general-fallback-v1/" + directory.name):
        raise ValueError("general native deployment identity differs")
    expected_files = payloads(attempt["remote"], engine_sha, identities)
    if attempt["payload_sha256"] != {name: hashlib.sha256(content).hexdigest()
            for name, content in expected_files.items()}:
        raise ValueError("general native payload manifest differs")
    for name, content in expected_files.items():
        if name != "libcrprobe.so" and (directory / name).read_bytes() != content:
            raise ValueError("general native archived payload differs")
    preflight = read("preflight.json")
    if (preflight["memory_available"] < 64 * 1024**3 or preflight["free_bytes"] < 5 * 1024**3
            or preflight["engine_sha256"] != engine_sha
            or preflight["formal_storage_gate_passed"] is not False):
        raise ValueError("general native resource preflight differs")
    job = JOBS[0]
    prefix = job["name"] + "/"
    cleanup = read(prefix + "cleanup.json")
    equal_fields(cleanup, dict(job=job, exit_code=0, error=None, clean=True),
        "general native engine cleanup failed")
    if read("run-results.json") != [cleanup] or [item["port"] for item in cleanup["engines"]] != job["ports"]:
        raise ValueError("general native cleanup registration differs")
    children = set()
    for item in cleanup["engines"]:
        process = item["process"]
        identity = process["phase_snapshot_engine"]
        if (process != read(prefix + f"engine-{item['port']}/process.json")
                or process["phase"] != "stopped" or process["port"] != item["port"]
                or item["pids_alive"] != dict(pid=False, child_pid=False) or item["listening"] is not False
                or identity["sha256"] != engine_sha or identity["path"] != attempt["remote"] + "/libcrprobe.so"
                or identity["launcher_sha256"] != attempt["payload_sha256"]["project/phase_snapshot_launcher.py"]
                or type(process["child_pid"]) is not int):
            raise ValueError("general native process identity or release differs")
        children.add(process["child_pid"])
    if len(children) != 2:
        raise ValueError("general native independent engines missing")
    native = read(prefix + "report.json")
    equal_fields(native, dict(schema="fair-final-general-provider-native-local.v1", status="passed",
        source_sha256=identities, checkpoint_sha256=SOURCE_SHA, tick=job["tick"], owner=job["owner"],
        limits=asdict(SearchLimits()), general_provider_sha256=sha(ROOT / "general_provider.py"),
        general_probe_sha256=sha(ROOT / "probe_native_general.py"),
        model_unchanged=True, actual_root_restored=True, snapshot_released=True,
        provider_closed=True, anchor_snapshot_released=True, native_pending_verified=False,
        native_fallback_verified=False, gates_closed=False, training_admissible=False, exclusions=EXCLUSIONS),
        "general native report identity or scope differs")
    duration(native["seconds"])
    records = native["records"]
    if [(row["stage"], row["altered"]) for row in records] != [
            (stage, altered) for stage in ("initial", "successor") for altered in (False, True)]:
        raise ValueError("general native paired roots missing")
    trace = json.loads(refill_runner.TRACE.read_text())
    if native["input_trace_sha256"] != sha(refill_runner.TRACE):
        raise ValueError("general native public trace identity differs")
    successor = next(group["tick"] for group in trace["boundaries"] if group["tick"] > job["tick"])
    histories = [FairHistory.from_payload(read(prefix + f"history-{stage}-0.json"))
        for stage in ("initial", "successor")]
    if ([history.root_tick for history in histories] != [job["tick"], successor]
            or not histories[1].extends(histories[0]) or any(history.rejections for history in histories)):
        raise ValueError("general native FAIR history progression differs")
    replayed = [dict(tick=group["tick"], fair_sha=group["fair_sha"])
        for group in trace["boundaries"] if group["tick"] < successor]
    if native["replayed"] != replayed or read(prefix + "progress.json") != dict(records=records, replayed=replayed):
        raise ValueError("general native archived trace differs")
    prior = FrozenDeckPrior.from_payload(read("project/public-deck-prior-engineering.json"))
    if native["prior_sha"] != prior.sha256:
        raise ValueError("general native deck prior differs")
    opponent = SimpleNamespace(checkpoint_sha=SOURCE_SHA, controller_sha=semantic_digest(
        dict(source=SOURCE_SHA, adapter="fair-final-source-a", temperature=1.)))
    factory = IncrementalRefillBeliefFactory(None, prior=prior, catalog=None, opponent=opponent, sidecar_factory=None)
    provider = GeneralHistorySearchProvider(factory, history=histories[0], anchor_seed=ANCHOR_SEED)
    try:
        first_anchor = records[0]["audit"]["world_manifest"]["construction"]["anchor"]
        conditional = split_seed(ANCHOR_SEED, "conditional-history", histories[0].sha256)
        accepted = [split_seed(conditional, f"attempt:{ordinal}") for ordinal in range(4)
            if semantic_digest([split_seed(conditional, f"attempt:{ordinal}")]) == first_anchor["seed_commitment"]]
        if len(accepted) != 1:
            raise ValueError("general native independent seed chain differs")
        completion = HistoryCycleCursor(prior, opening=histories[0].opening).sample(histories[0],
            seed=split_seed(accepted[0], "opponent-draw"))
        for index, row in enumerate(records):
            history = histories[index // 2]
            if read(prefix + f"history-{row['stage']}-{int(row['altered'])}.json") != payload(history.to_payload()):
                raise ValueError("general native hidden intervention changed history")
            group = next(group for group in trace["boundaries"] if group["tick"] == history.root_tick)
            if history.root_fair_sha != group["fair_sha"][str(job["owner"])]:
                raise ValueError("general native FAIR frame differs from trace")
            expected_decisions = [dict(tick=group["tick"], fair_sha=group["fair_sha"][str(job["owner"])],
                action=group["actions"][str(job["owner"])] ) for group in trace["boundaries"]
                if group["tick"] < history.root_tick and str(job["owner"]) in group["fair_sha"]]
            deployments = sorted([dict(tick=event["tick"], owner=event["owner"], card_id=event["card_id"],
                x=int(event["position"][0]), y=int(event["position"][1]))
                for tick in trace["ticks"] if history.opening.tick < tick["tick"] <= history.root_tick
                for event in tick["public_executions"]], key=lambda event: (event["tick"], event["owner"]))
            if (payload([asdict(decision) for decision in history.decisions]) != expected_decisions
                    or payload([asdict(event) for event in history.deployments]) != deployments):
                raise ValueError("general native recorded history differs from public trace")
            if index == 2:
                completion = advance_completion(completion,
                    history.deployments[len(histories[0].deployments):], owner=1 - job["owner"])
            provider.history = history
            validate_world(row, history, provider, opponent)
            validate_layouts(row, history, completion)
            previous = histories[0].opening.tick if index == 0 else histories[0 if index < 3 else 1].root_tick
            equal_fields(row["local_profile"]["anchor_event"], dict(
                mode="cold" if index == 0 else "incremental", status="verified",
                from_tick=previous, to_tick=history.root_tick, reason=None),
                "general native incremental cursor differs")
            equal_fields(row["anchor_profile"], dict(
                cold_ticks=histories[0].root_tick - histories[0].opening.tick,
                incremental_ticks=0 if index < 2 else histories[1].root_tick - histories[0].root_tick,
                ticks=history.root_tick - history.opening.tick, syncs=index + 1),
                "general native replay budget differs")
        if native["cursor_events"] != [row["local_profile"]["anchor_event"] for row in records]:
            raise ValueError("general native incremental cursor evidence differs")
        for pair in (records[:2], records[2:]):
            for key in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle",
                    "anchor_completion", "anchor_layouts", "local_layouts"):
                if pair[0][key] != pair[1][key]:
                    raise ValueError("general native actual hidden state influenced search")
            if pair[0]["actual_digest_before"] != pair[1]["mutation"]["before_digest"]:
                raise ValueError("general native hidden mutation baseline differs")
    finally:
        provider.close()
        factory.close()
    imports = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
        if name.startswith(("native_runner", "tools.probe_fair_final")) and getattr(module, "__file__", None)}
    if any(not Path(path).is_relative_to(PROJECT) for path in imports.values()):
        raise ValueError("general native validator imported another runtime version")
    return dict(schema="fair-final-general-native-local-verification.v1", status="passed_limited_scope",
        searches=4, valid_simulations=512, hidden_pairs=2, independent_engines=2, engine_cleanup_passed=True,
        native_local_path_verified=True, native_fallback_verified=False, native_pending_verified=False,
        complete_game_verified=False, gates_closed=False, training_admissible=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    report = validate(args.attempt)
    report["verifier_sha256"] = sha(Path(__file__))
    report["artifacts"] = {str(path.relative_to(args.attempt)): sha(path)
        for path in sorted(args.attempt.rglob("*")) if path.is_file() and path.name != "verification.json"}
    (args.attempt / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "artifacts"}), flush=True)


if __name__ == "__main__":
    main()
