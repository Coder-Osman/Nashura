from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2]/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.prior_history import HistoryCycleCursor
from native_runner.training.fair_final.refill_belief import verify_completion_layout
from native_runner.training.fair_final.refill_local import (
    IncrementalRefillBeliefFactory, RefillDrawReconditioner, advance_completion, unseen_relabeling)
from native_runner.training.fair_final.search import split_seed
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from verify_local_reconditioning import validate_phase_engine_build
from verify_refill import layout_from_record, validate_refill_trajectory


MODEL_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
SCOPE = "anchor-conditional-unseen-permutations-not-full-posterior"
EXCLUSIONS = ["full-posterior-resampling", "general-pending-plan", "Full128-local-search",
    "complete-game-search", "throughput-ab", "training-admission"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload(value):
    return json.loads(json.dumps(value))


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("refill local duration is invalid")


def relabeled_layout(record, mapping, slots):
    layout = layout_from_record(record)
    hand = [None if card is None else mapping[card] for card in layout.hand]
    cycle = [mapping[card] for card in layout.cycle]
    return dict(hand=hand, cycle=cycle, hand_slots=[-1 if card is None else slots[card] for card in hand],
        cycle_slots=[slots[card] for card in cycle])


def validate_continuation(continuation, anchor, tick):
    require_sha(continuation["digest"])
    if len(continuation["ticks"]) != 30:
        raise ValueError("refill continuation must cover thirty native ticks")
    previous = [layout_from_record(record) for record in anchor]
    for offset, row in enumerate(continuation["ticks"], 1):
        if row["tick"] != tick+offset or len(row["layouts"]) != 2:
            raise ValueError("refill continuation skipped time or a player")
        current = [layout_from_record(record) for record in row["layouts"]]
        for old, new in zip(previous, current):
            drawn = len(old.cycle)-len(new.cycle)
            if (not 0 <= drawn <= len(old.holes) or new.cycle != old.cycle[drawn:]
                    or new.cycle_slots != old.cycle_slots[drawn:]
                    or any(old.hand[index] is not None and
                        (new.hand[index], new.hand_slots[index]) != (old.hand[index], old.hand_slots[index])
                        for index in range(4))
                    or {(new.hand[index], new.hand_slots[index]) for index in old.holes if new.hand[index] is not None}
                        != set(zip(old.cycle[:drawn], old.cycle_slots[:drawn]))):
                raise ValueError("WAIT continuation changed refill FIFO or occupied slots")
        previous = current
    if any(layout.holes for layout in previous):
        raise ValueError("refill continuation did not reach completed refills")


def validate_refill_local(report=None, *, root=ROOT):
    try:
        return validate(report, root=root)
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError("refill local evidence is incomplete or malformed") from error


def validate(report, *, root):
    from run_native_probe import BASE
    from run_refill_local_validation import BUILD, INPUT, JOBS, successor_histories

    if report is None:
        report = json.loads((root/"native-refill-local-report.json").read_text())
    if (report["schema"] != "fair-final-refill-local-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["jobs"] != JOBS
            or [shard["job"] for shard in report["shards"]] != JOBS
            or report["purpose"] != "refill-unseen-relabeling-and-real-history-successor"
            or not report["artifacts"] or not report["source_sha256"]):
        raise ValueError("refill local suite identity differs")
    for name, key in (("run_refill_local_validation.py", "runner_sha256"), ("refill_local_worker.py", "worker_sha256")):
        if sha(root/name) != report[key]:
            raise ValueError("refill local deployment code changed")
    expected_sources = {str(path.relative_to(SOURCE)): sha(path)
        for path in (SOURCE/"native_runner/training/fair_final").glob("*.py")}
    expected_sources.update({name: sha(SOURCE/name) for name in
        ("native_runner/training_capture.py", "tools/probe_fair_final_refill_local.py")})
    if report["source_sha256"] != expected_sources:
        raise ValueError("refill local source identity changed")
    for relative, digest in report["artifacts"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts or sha(root/relative) != digest:
            raise ValueError("refill local artifact changed or escapes evidence")

    def path(suffix):
        matches = [root/name for name in report["artifacts"] if name.endswith("/"+suffix)]
        if len(matches) != 1:
            raise ValueError("refill local artifact missing or ambiguous: "+suffix)
        return matches[0]

    def read(suffix):
        return json.loads(path(suffix).read_text())

    trajectory = validate_refill_trajectory()
    successors = successor_histories()
    engine_sha = validate_phase_engine_build(read("engine-build-manifest.json"))
    registration, attempt, preflight = read("run-manifest.json"), read("attempt.json"), read("preflight.json")
    settings = dict(root=report["remote"], base=BASE, jobs=JOBS, engine_sha256=engine_sha,
        engine_path=report["remote"]+"/libcrprobe.so")
    uploads = {"project/run.py": (root/"refill_local_worker.py").read_bytes(),
        **{"project/"+job["history"]: (INPUT/job["history"]).read_bytes() for job in JOBS},
        **{"project/"+name: content for name, content in successors.items()},
        "project/public-deck-prior-engineering.json": (root/"public-deck-prior-engineering.json").read_bytes(),
        "project/probe-source-manifest.json": json.dumps(expected_sources, sort_keys=True).encode(),
        "run-manifest.json": json.dumps(settings).encode(), "libcrprobe.so": (BUILD/"libcrprobe.so").read_bytes(),
        "engine-build-manifest.json": (BUILD/"build-manifest.json").read_bytes(),
        "project/phase_snapshot_launcher.py": (root/"phase_snapshot_launcher.py").read_bytes()}
    expected_payload = {name: hashlib.sha256(content).hexdigest() for name, content in uploads.items()}
    if (registration != settings or attempt["exit_code"] != 0
            or not report["remote"].startswith("/home/zhichun/fair-final-v1-20261019-refill-local/native-refill-local-")
            or any(attempt[key] != report[key] for key in ("remote", "jobs", "purpose", "source_sha256",
                "runner_sha256", "worker_sha256", "engine_sha256", "payload_sha256"))
            or report["payload_sha256"] != expected_payload
            or preflight["engine_sha256"] != engine_sha or report["engine_sha256"] != engine_sha
            or preflight["memory_available"] < 64*1024**3 or preflight["free_bytes"] < 5*1024**3):
        raise ValueError("refill local deployed payload, engine or resources differ")
    for name, digest in expected_payload.items():
        if name.endswith(".json") and sha(path(name)) != digest:
            raise ValueError("refill local uploaded JSON changed")
    opponent = SimpleNamespace(checkpoint_sha=MODEL_SHA,
        controller_sha=semantic_digest(dict(source=MODEL_SHA, adapter="fair-final-source-a", temperature=1.)))
    prior = FrozenDeckPrior.from_payload(read("project/public-deck-prior-engineering.json"))
    factory = IncrementalRefillBeliefFactory(lambda: None, prior=prior, catalog=None,
        opponent=opponent, sidecar_factory=None)
    conditioner = RefillDrawReconditioner(factory)
    results, children = read("run-results.json"), set()
    if results != [shard["cleanup"] for shard in report["shards"]]:
        raise ValueError("refill local worker outcomes differ")
    for shard in report["shards"]:
        job, native, cleanup = shard["job"], shard["report"], shard["cleanup"]
        prefix = job["name"]+"/"
        if (read(prefix+"report.json") != native or read(prefix+"cleanup.json") != cleanup
                or cleanup["job"] != job or cleanup["exit_code"] != 0 or cleanup["error"] is not None
                or cleanup["clean"] is not True or cleanup["listening"] is not False
                or cleanup["pids_alive"] != dict(pid=False, child_pid=False)):
            raise ValueError("refill local raw result or engine release differs")
        process = cleanup["process"]
        identity = process["phase_snapshot_engine"]
        if (process != read(prefix+f"engine-{job['port']}/process.json") or process["phase"] != "stopped"
                or process["port"] != job["port"] or identity["sha256"] != engine_sha
                or identity["path"] != registration["engine_path"]
                or identity["launcher_sha256"] != expected_payload["project/phase_snapshot_launcher.py"]
                or process["argv"].count(registration["engine_path"]) != 1
                or type(process["child_pid"]) is not int or process["child_pid"] in children):
            raise ValueError("refill local engine process identity differs")
        require_sha(identity["service_sha256"])
        children.add(process["child_pid"])
        if (native["schema"] != "fair-final-refill-local-native.v1" or native["status"] != "passed"
                or native["training_admissible"] is not False or native["source_sha256"] != expected_sources
                or native["checkpoint_sha256"] != MODEL_SHA or native["sampler_sha"] != conditioner.sampler_sha
                or native["prior_sha"] != prior.sha256 or native["exclusions"] != EXCLUSIONS
                or native["model_unchanged"] is not True or native["post_mutation_rollback_verified"] is not True):
            raise ValueError("refill local native scope or frozen model differs")
        duration(native["seconds"])
        histories = [FairHistory.from_payload(read("project/"+job[key])) for key in ("history", "next_history")]
        original_sha = next(digest for relative, digest in trajectory["artifacts"].items()
            if relative.endswith("/trajectory/"+job["history"]))
        if (sha(path("project/"+job["history"])) != original_sha or not histories[1].extends(histories[0])
                or histories[0].root_tick != job["tick"] or histories[0].opening.owner != job["owner"]):
            raise ValueError("refill local history differs from accepted public trajectory")
        records, syncs = native["records"], native["syncs"]
        expected_rows = [(history.root_tick, job["owner"], seed) for history in histories
            for seed in (78102501, 78102502, 78102501)]
        if ([(row["tick"], row["owner"], row["seed"]) for row in records] != expected_rows
                or len(syncs) != 2 or read(prefix+"progress.json") != dict(records=records, syncs=syncs)
                or len(native["rollbacks"]) != 2):
            raise ValueError("refill local root coverage, progress or rollback missing")
        cold = syncs[0]["construction_manifest"]
        attempts = [split_seed(78102401, f"attempt:{index}") for index in range(4)]
        seeds = [seed for seed in attempts if semantic_digest([seed]) == cold["seed_commitment"]]
        if len(seeds) != 1:
            raise ValueError("refill local cold construction seed differs")
        completion = HistoryCycleCursor(prior, opening=histories[0].opening).sample(histories[0],
            seed=split_seed(seeds[0], "opponent-draw"))
        for index, history in enumerate(histories):
            if index:
                completion = advance_completion(completion,
                    history.deployments[len(histories[0].deployments):], owner=1-job["owner"])
            sync = syncs[index]
            construction = sync["construction_manifest"]
            from_tick = histories[0].root_tick if index else history.opening.tick
            ticks = history.root_tick-from_tick
            last = sync["profile"]["last_sync"]
            if (sync["mode"] != ("incremental" if index else "cold") or sync["history_sha"] != history.sha256
                    or last["from_tick"] != from_tick or last["to_tick"] != history.root_tick or last["ticks"] != ticks
                    or sync["profile"]["incremental_ticks"] != (ticks if index else 0)
                    or sync["profile"]["cold_ticks"] != histories[0].root_tick-history.opening.tick
                    or sync["profile"]["ticks"] != history.root_tick-history.opening.tick
                    or sync["profile"]["syncs"] != index+1
                    or construction["source"] != "independent-belief" or construction["actual_state_copied"] is not False
                    or construction["history_sha"] != history.sha256 or construction["root_fair_sha"] != history.root_fair_sha
                    or construction["root_tick"] != history.root_tick or construction["owner"] != job["owner"]
                    or construction["opponent_checkpoint_sha"] != MODEL_SHA
                    or construction["opponent_controller_sha"] != opponent.controller_sha
                    or construction["training_admissible"] is not False
                    or construction["sampler_sha"] != factory.sampler_sha or construction["prior_sha"] != prior.sha256
                    or construction["native_draw_verified"] is not True
                    or construction["completion_manifest"] != payload(completion.manifest)):
                raise ValueError("refill local direct incremental construction differs")
            duration(sync["seconds"])
            duration(last["seconds"])
            signatures = {}
            group = records[index*3:(index+1)*3]
            for row in group:
                if row["anchor_completion"] != payload(asdict(completion)):
                    raise ValueError("refill local anchor draw differs from independently sampled prior")
                mapping, changed = unseen_relabeling(completion, history, seed=row["seed"])
                if row["completion"] != payload(asdict(changed)):
                    raise ValueError("refill local public-fixed completion differs")
                anchor = [layout_from_record(record) for record in row["anchor_layouts"]]
                if len(anchor) != 2:
                    raise ValueError("refill local anchor player missing")
                verify_completion_layout(anchor[1-job["owner"]], completion, history)
                slots = {card: slot for card, slot in zip(
                    (*anchor[1-job["owner"]].hand, *anchor[1-job["owner"]].cycle),
                    (*anchor[1-job["owner"]].hand_slots, *anchor[1-job["owner"]].cycle_slots)) if card is not None}
                expected_layouts = [record if seat == job["owner"] else relabeled_layout(record, mapping, slots)
                    for seat, record in enumerate(row["anchor_layouts"])]
                if row["local_layouts"] != expected_layouts:
                    raise ValueError("refill local slots, holes or own cards changed")
                verify_completion_layout(layout_from_record(row["local_layouts"][1-job["owner"]]), changed, history)
                manifest = row["manifest"]
                if (manifest["schema"] != conditioner.schema or manifest["source"] != "independent-belief"
                        or manifest["actual_state_copied"] is not False or manifest["training_admissible"] is not False
                        or manifest["scope"] != SCOPE or manifest["history_ticks_replayed"] != 0
                        or manifest["history_sha"] != history.sha256 or manifest["root_fair_sha"] != history.root_fair_sha
                        or manifest["owner"] != job["owner"] or manifest["root_tick"] != history.root_tick
                        or manifest["sampler_sha"] != conditioner.sampler_sha or manifest["prior_sha"] != prior.sha256
                        or manifest["completion_manifest"] != payload(changed.manifest)
                        or manifest["native_draw_verified"] is not True or manifest["controller_unchanged"] is not True
                        or manifest["public_and_own_fair_unchanged"] is not True
                        or manifest["native_current_draw"] != [*expected_layouts[1-job["owner"]]["hand"],
                            *expected_layouts[1-job["owner"]]["cycle"]]
                        or manifest["refill_holes"] != list(anchor[1-job["owner"]].holes)
                        or row["anchor_restored"] is not True or row["refill_timing_unchanged"] is not True
                        or len(row["continuations"]) != 2 or row["continuations"][0] != row["continuations"][1]):
                    raise ValueError("refill local manifest, restoration or repeated continuation differs")
                duration(row["seconds"])
                require_sha(row["anchor_digest"])
                require_sha(row["local_digest"])
                validate_continuation(row["baseline"], row["anchor_layouts"], history.root_tick)
                validate_continuation(row["continuations"][0], row["local_layouts"], history.root_tick)
                for before, after in zip(row["baseline"]["ticks"], row["continuations"][0]["ticks"]):
                    expected = [record if seat == job["owner"] else relabeled_layout(record, mapping, slots)
                        for seat, record in enumerate(before["layouts"])]
                    if after["tick"] != before["tick"] or after["layouts"] != expected:
                        raise ValueError("refill local transformation changed baseline refill timing")
                signature = {key: value for key, value in row.items() if key not in ("seconds",)}
                if row["seed"] in signatures and signatures[row["seed"]] != signature:
                    raise ValueError("refill local A/B/A depends on preceding sample")
                signatures[row["seed"]] = signature
            if any(row["anchor_digest"] != group[0]["anchor_digest"] or row["anchor_layouts"] != group[0]["anchor_layouts"]
                    or row["baseline"] != group[0]["baseline"] for row in group):
                raise ValueError("refill local samples did not share a restored independent anchor")
            rollback = native["rollbacks"][index]
            if rollback != dict(tick=history.root_tick, owner=job["owner"], calls=3,
                    anchor_digest=group[0]["anchor_digest"], restored_digest=group[0]["anchor_digest"]):
                raise ValueError("refill local post-mutation failure did not restore its anchor")
    factory.close()
    return report


if __name__ == "__main__":
    result = validate_refill_local()
    print(json.dumps(dict(status="passed", shards=len(result["shards"]), roots=12,
        local_replacements=36, training_admissible=False)))
