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

from native_runner.contracts import EventV1
from native_runner.training.fair_final.history import FairHistory, PublicDeployment
from native_runner.training.fair_final.refill_belief import native_draw_layout
from verify_local_reconditioning import validate_phase_engine_build


def validate_suite(report, *, root, runner, worker, schema, purpose, jobs):
    if (report["schema"] != schema or report["status"] != "passed"
            or report["training_admissible"] is not False or report["purpose"] != purpose
            or report["jobs"] != jobs or [shard["job"] for shard in report["shards"]] != jobs):
        raise ValueError("refill suite scope or job identity differs")
    for filename, key in ((runner, "runner_sha256"), (worker, "worker_sha256")):
        if hashlib.sha256((root/filename).read_bytes()).hexdigest() != report[key]:
            raise ValueError("refill deployment code identity differs")
    if not report["artifacts"] or not report["source_sha256"]:
        raise ValueError("refill evidence manifest empty")
    for base, mapping in ((root, report["artifacts"]), (SOURCE, report["source_sha256"])):
        for relative, digest in mapping.items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("refill evidence path escapes root")
            if hashlib.sha256((base/relative).read_bytes()).hexdigest() != digest:
                raise ValueError("refill source or artifact changed")

    def path(suffix):
        paths = [root/relative for relative in report["artifacts"] if relative.endswith("/"+suffix)]
        if len(paths) != 1:
            raise ValueError("refill artifact missing or ambiguous: "+suffix)
        return paths[0]

    def read(suffix):
        return json.loads(path(suffix).read_text())

    engine_sha = validate_phase_engine_build(read("engine-build-manifest.json"))
    attempt, registration, preflight = read("attempt.json"), read("run-manifest.json"), read("preflight.json")
    if (attempt["exit_code"] != 0 or registration["jobs"] != jobs or registration["root"] != report["remote"]
            or registration["engine_path"] != report["remote"]+"/libcrprobe.so"
            or any(attempt[key] != report[key] for key in ("remote", "jobs", "purpose", "source_sha256",
                "runner_sha256", "worker_sha256", "engine_sha256"))
            or any(value != engine_sha for value in (registration["engine_sha256"],
                preflight["engine_sha256"], report["engine_sha256"]))
            or preflight["memory_available"] < 64*1024**3 or preflight["free_bytes"] < 5*1024**3
            or read("project/probe-source-manifest.json") != report["source_sha256"]
            or hashlib.sha256(path("project/run.py").read_bytes()).hexdigest() != report["worker_sha256"]):
        raise ValueError("refill deployment identity or resources differ")
    launcher_sha = hashlib.sha256((root/"phase_snapshot_launcher.py").read_bytes()).hexdigest()
    if hashlib.sha256(path("project/phase_snapshot_launcher.py").read_bytes()).hexdigest() != launcher_sha:
        raise ValueError("refill launcher differs")
    results = read("run-results.json")
    if ([entry["job"] for entry in results] != jobs or any(entry["exit_code"] != 0
            or entry["error"] is not None or entry["clean"] is not True for entry in results)):
        raise ValueError("refill worker failed")
    children = set()
    for shard in report["shards"]:
        prefix = shard["job"]["name"]+"/"
        native, cleanup = shard["report"], shard["cleanup"]
        if (read(prefix+"report.json") != native or native["status"] != "passed"
                or native["training_admissible"] is not False or native["model_unchanged"] is not True
                or native["source_sha256"] != report["source_sha256"]
                or native["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
                or read(prefix+"cleanup.json") != cleanup or cleanup["clean"] is not True
                or cleanup["exit_code"] != 0 or cleanup["error"] is not None
                or [engine["port"] for engine in cleanup["engines"]] != shard["job"]["ports"]):
            raise ValueError("refill native identity or cleanup differs")
        for engine in cleanup["engines"]:
            process = engine["process"]
            identity = process["phase_snapshot_engine"]
            if (engine["listening"] or any(engine["pids_alive"].values()) or process["phase"] != "stopped"
                    or identity["sha256"] != engine_sha or identity["launcher_sha256"] != launcher_sha
                    or identity["path"] != registration["engine_path"]
                    or read(prefix+f"engine-{engine['port']}/process.json") != process):
                raise ValueError("refill native engine identity or release differs")
            children.add(process["child_pid"])
    if len(children) != sum(len(job["ports"]) for job in jobs):
        raise ValueError("refill jobs shared native processes")
    return path, read


def validate_trajectory_rows(native, trace):
    if (native["schema"] != "fair-final-refill-trajectory.v1" or native["result"] != trace["result"]
            or native["engine_seed"] != 78101906 or native["execution_seed"] != 78101946
            or [row["tick"] for row in native["roots"]] != [110, 2598, 5602]):
        raise ValueError("refill trajectory terminal or root coverage differs")
    changes = native["changes"]
    ticks = [row["tick"] for row in changes]
    if not changes or ticks != sorted(set(ticks)) or ticks[0] != trace["opening_tick"]:
        raise ValueError("refill draw transitions missing or reordered")
    events = {row["tick"]: row["public_executions"] for row in trace["ticks"] if row["public_executions"]}
    if not set(events) <= set(ticks):
        raise ValueError("public play omitted from refill changes")
    previous = None
    for row in changes:
        layouts = {player["owner"]: native_draw_layout(player) for player in row["players"]}
        if len(row["players"]) != 2 or set(layouts) != {0, 1}:
            raise ValueError("refill player coverage differs")
        if previous is not None:
            for owner in (0, 1):
                old, new = previous[owner], layouts[owner]
                hand = {card for card in old.hand if card is not None}
                queue = list(old.cycle)
                for event in events.get(row["tick"], ()):
                    if event["owner"] == owner:
                        if event["card_id"] not in hand:
                            raise ValueError("public action played an unavailable card")
                        hand.remove(event["card_id"])
                        queue.append(event["card_id"])
                refills = len(queue)-len(new.cycle)
                if (not 0 <= refills <= 4-len(hand) or tuple(queue[refills:]) != new.cycle
                        or hand | set(queue[:refills]) != {card for card in new.hand if card is not None}):
                    raise ValueError("native delayed refill violates FIFO conservation")
        previous = layouts
    for root in native["roots"]:
        if (root["players"] != next(row["players"] for row in changes if row["tick"] == root["tick"])
                or root["restored"] is not True or root["continuations"][0] != root["continuations"][1]
                or len(root["continuations"]) != 2 or len(root["continuations"][0]) != 30):
            raise ValueError("refill snapshot restoration or repeated continuation differs")
        root_layouts = {player["owner"]: native_draw_layout(player) for player in root["players"]}
        if len(root_layouts[1].holes) != 1 or root_layouts[0].holes:
            raise ValueError("registered one-hole refill case missing")
        for offset, row in enumerate(root["continuations"][0], 1):
            if row["tick"] != root["tick"]+offset:
                raise ValueError("refill continuation skipped a native tick")
            for player in row["players"]:
                layout = native_draw_layout(player)
                original = root_layouts[player["owner"]]
                refills = len(original.cycle)-len(layout.cycle)
                if (not 0 <= refills <= len(original.holes)
                        or layout.cycle != original.cycle[refills:]
                        or {card for card in layout.hand if card is not None} !=
                            {card for card in original.hand if card is not None} | set(original.cycle[:refills])):
                    raise ValueError("WAIT continuation changed delayed refill FIFO")
        if any(native_draw_layout(player).holes for player in root["continuations"][0][-1]["players"]):
            raise ValueError("refill continuation did not cover completed refill")


def validate_refill_trajectory(report=None, *, root=ROOT):
    from run_refill_validation import JOBS
    from verify_boundary_coverage import validate_boundary_coverage

    if report is None:
        report = json.loads((root/"native-refill-trajectory-report.json").read_text())
    path, read = validate_suite(report, root=root, runner="run_refill_validation.py", worker="refill_worker.py",
        schema="fair-final-refill-trajectory-suite.v1", purpose="refill-shape-and-snapshot-trajectory", jobs=JOBS)
    original = validate_boundary_coverage()
    original_sha = original["shards"][0]["report"]["trace_sha256"]
    trace = read("project/input-trace.json")
    native = report["shards"][0]["report"]
    if native["input_trace_sha256"] != original_sha or hashlib.sha256(path("project/input-trace.json").read_bytes()).hexdigest() != original_sha:
        raise ValueError("refill input differs from accepted trajectory")
    validate_trajectory_rows(native, trace)
    deployments = [PublicDeployment.from_event(EventV1.from_mapping(event))
        for row in trace["ticks"] for event in row["public_executions"]]
    groups = {row["tick"]: row for row in trace["boundaries"]}
    for row in native["roots"]:
        if row["fair_sha"] != groups[row["tick"]]["fair_sha"]:
            raise ValueError("refill root FAIR identity differs")
        for owner in (0, 1):
            history = FairHistory.from_payload(read(f"trajectory/history-{row['tick']}-{owner}.json"))
            if (history.opening.owner != owner or history.root_tick != row["tick"]
                    or history.root_fair_sha != row["fair_sha"][str(owner)]
                    or history.deployments != tuple(sorted(event for event in deployments if event.tick <= row["tick"]))
                    or [entry.tick for entry in history.decisions] != [tick for tick in groups if tick < row["tick"]]
                    or any(entry.fair_sha != groups[entry.tick]["fair_sha"][str(owner)]
                        or asdict(entry.action) != groups[entry.tick]["actions"][str(owner)] for entry in history.decisions)):
                raise ValueError("refill FAIR history differs from accepted public trajectory")
    return report


def layout_from_record(record):
    hand, cycle = record["hand"], record["cycle"]
    hand_slots, cycle_slots = record["hand_slots"], record["cycle_slots"]
    if len(hand) != 4 or len(hand_slots) != 4 or len(cycle) != len(cycle_slots):
        raise ValueError("recorded refill layout shape differs")
    if any((card is None) != (slot == -1) for card, slot in zip(hand, hand_slots)):
        raise ValueError("recorded refill holes and native slots differ")
    cards = [(card, slot) for card, slot in zip(hand, hand_slots) if card is not None] + list(zip(cycle, cycle_slots))
    player = dict(deck=[dict(cardId=card, deckSlot=slot) for card, slot in cards],
        hand=[dict(cardId=card, deckSlot=slot, handIndex=index)
            for index, (card, slot) in enumerate(zip(hand, hand_slots)) if card is not None],
        cycle=[dict(cardId=card, deckSlot=slot, cycleIndex=index)
            for index, (card, slot) in enumerate(zip(cycle, cycle_slots))])
    return native_draw_layout(player)


def validate_refill_search(report=None, *, root=ROOT):
    from run_refill_search_validation import JOBS
    from native_runner.training.fair_final.codec import ActionCodec
    from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
    from native_runner.training.fair_final.prior_history import HistoryCycleCursor
    from native_runner.training.fair_final.prior_provider import PriorHistorySearchProvider
    from native_runner.training.fair_final.refill_belief import RefillPriorHistoryBeliefFactory, verify_completion_layout
    from native_runner.training.fair_final.search import SearchLimits, split_seed
    from native_runner.training.fair_search.config import HOG_DECK
    from native_runner.training.fair_search.contracts import semantic_digest
    from verify_prior_search import validate_attempts

    if report is None:
        report = json.loads((root/"native-refill-search-report.json").read_text())
    path, read = validate_suite(report, root=root, runner="run_refill_search_validation.py",
        worker="refill_search_worker.py", schema="fair-final-refill-search-suite.v1",
        purpose="refill-independent-history-and-Full128", jobs=JOBS)
    trajectory = validate_refill_trajectory()
    prior_payload = read("project/public-deck-prior-engineering.json")
    if prior_payload != json.loads((ROOT/"public-deck-prior-engineering.json").read_text()):
        raise ValueError("refill search changed public deck prior")
    prior = FrozenDeckPrior.from_payload(prior_payload)
    source_sha = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
    opponent = SimpleNamespace(checkpoint_sha=source_sha,
        controller_sha=semantic_digest(dict(source=source_sha, adapter="fair-final-source-a", temperature=1.)))
    limits, codec = SearchLimits(), ActionCodec(HOG_DECK)
    for shard in report["shards"]:
        job, native = shard["job"], shard["report"]
        history_path = path("project/"+job["history"])
        history = FairHistory.from_payload(json.loads(history_path.read_text()))
        original_history_sha = next(digest for relative, digest in trajectory["artifacts"].items()
            if relative.endswith("/trajectory/"+job["history"]))
        if (native["schema"] != "fair-final-refill-search.v1" or native["owner"] != job["owner"]
                or native["tick"] != job["tick"] or history.opening.owner != job["owner"]
                or history.root_tick != job["tick"] or native["history_sha"] != history.sha256
                or native["history_sha256"] != original_history_sha
                or hashlib.sha256(history_path.read_bytes()).hexdigest() != original_history_sha
                or native["limits"] != asdict(limits)
                or native["exclusions"] != ["local-refill-reconditioning", "actual-hidden-state-intervention",
                    "general-pending-plan", "complete-game-search", "training-admission"]):
            raise ValueError("refill search root, history, horizon or scope differs")
        reconstruction = native["reconstruction"]
        if (read(job["name"]+"/reconstruction.json") != reconstruction
                or len(reconstruction["continuation"]) != 2
                or reconstruction["continuation"][0] != reconstruction["continuation"][1]):
            raise ValueError("refill reconstruction continuation differs")
        factory = RefillPriorHistoryBeliefFactory(None, prior=prior, catalog=None,
            opponent=opponent, sidecar_factory=None)
        provider = PriorHistorySearchProvider(factory, history=history)
        world_seed = split_seed(78102403, "world")
        seeds = (78102401, split_seed(world_seed, "conditional-history", history.sha256))
        manifests = (reconstruction["manifest"], native["audit"]["world_manifest"]["conditioning"])
        for stage, (seed, manifest, rows) in enumerate(zip(seeds, manifests,
                (reconstruction["layouts"], native["search_layouts"]))):
            accepted = [split_seed(seed, f"attempt:{index}") for index in range(4)
                if semantic_digest([split_seed(seed, f"attempt:{index}")]) == manifest["seed_commitment"]]
            if len(accepted) != 1 or len(rows) != 2 or {row["owner"] for row in rows} != {0, 1}:
                raise ValueError("refill completion seed or player coverage differs")
            layouts = {row["owner"]: layout_from_record(row) for row in rows}
            completion = HistoryCycleCursor(prior, opening=history.opening).sample(history,
                seed=split_seed(accepted[0], "opponent-draw"))
            enemy = layouts[1-job["owner"]]
            verify_completion_layout(enemy, completion, history)
            expected = dict(source="independent-belief", actual_state_copied=False,
                root_fair_sha=history.root_fair_sha, history_sha=history.sha256, owner=job["owner"],
                root_tick=job["tick"], sampler_sha=factory.sampler_sha, prior_sha=prior.sha256,
                native_draw_verified=True, native_draw_schema="native-holes-delayed-fifo.v1",
                native_current_draw=[*enemy.hand, *enemy.cycle], refill_holes=list(enemy.holes),
                completion_manifest=completion.manifest, training_admissible=False,
                opponent_controller_sha=opponent.controller_sha, opponent_checkpoint_sha=source_sha)
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise ValueError("refill conditioned world identity differs")
            conditioning = manifest.get("hidden_plan_conditioning")
            if (not isinstance(conditioning, dict) or set(conditioning) != {"calls", "minimum_log_mass", "maximum_prefixes"}
                    or type(conditioning["calls"]) is not int or conditioning["calls"] < 0
                    or type(conditioning["maximum_prefixes"]) is not int):
                raise ValueError("refill hidden-plan conditioning evidence missing or malformed")
            if conditioning["calls"]:
                mass = conditioning["minimum_log_mass"]
                if (type(mass) not in (int, float) or not math.isfinite(mass) or mass > 1e-5
                        or not 1 <= conditioning["maximum_prefixes"] <= 2+8+8*576):
                    raise ValueError("refill hidden-plan conditional mass or finite work bound differs")
            elif conditioning["minimum_log_mass"] is not None or conditioning["maximum_prefixes"] != 0:
                raise ValueError("unused hidden-plan sampler reported work")
            original = next(row for row in trajectory["shards"][0]["report"]["roots"] if row["tick"] == job["tick"])
            own_reference = native_draw_layout(next(player for player in original["players"] if player["owner"] == job["owner"]))
            own = layouts[job["owner"]]
            if own.hand != own_reference.hand or own.cycle != own_reference.cycle:
                raise ValueError("independent refill world changed known own draw")
            if stage == 0:
                for owner, following in enumerate(reconstruction["continuation"][0]["layouts"]):
                    end = layout_from_record(following)
                    if end.holes or set(end.logical_hand) != set(layouts[owner].logical_hand) or end.cycle != layouts[owner].logical_cycle:
                        raise ValueError("independent WAIT continuation did not preserve refill FIFO")
        result, audit = native["outcome"]["result"], native["audit"]
        world = audit["world_manifest"]
        if (result["schema"] != "SearchResult/v1" or result["valid_simulations"] != 128
                or result["root_player"] != job["owner"] or result["value_perspective"] != job["owner"]
                or result["root_fair_state_sha"] != history.root_fair_sha or result["search_protocol_sha"] != limits.sha256
                or result["noise_fraction"] != 0 or result["unfinished_edge_backups"] != 0
                or result["world_sampler_sha"] != provider.sampler_sha
                or result["opponent_controller_sha"] != opponent.controller_sha
                or world["source"] != "independent-belief" or world["actual_state_copied"] is not False
                or world["training_admissible"] is not False or world["root_fair_sha"] != history.root_fair_sha
                or world["history_sha"] != history.sha256 or world["sampler_sha"] != provider.sampler_sha
                or world["seed_commitment"] != semantic_digest([world_seed])
                or world["root_sampling"] != "fresh-independent-world-per-build"
                or result["world_commitment"] != semantic_digest(world)):
            raise ValueError("refill search budget or world identity differs")
        visits = {row["action_id"]: row["visits"] for row in result["candidates"]}
        if (not 1 <= len(visits) <= 32 or len(visits) != len(result["candidates"])
                or any(type(count) is not int or count < 0 for count in visits.values()) or sum(visits.values()) != 128
                or native["outcome"]["target"] != [[action, visits[action]] for action in sorted(visits) if visits[action]]):
            raise ValueError("refill visit target differs")
        positive = sorted(action for action in visits if visits[action])
        chosen = random.Random(split_seed(78102404, "behavior")).choices(positive,
            weights=[visits[action] for action in positive], k=1)[0]
        if codec.decode(chosen).key != native["outcome"]["selected"]:
            raise ValueError("refill execution sampling differs")
        for row in result["candidates"]:
            codec.decode(row["action_id"])
            if (any(not math.isfinite(row[key]) for key in ("q", "original_joint_logp", "prior_card", "prior_projected", "prior_search"))
                    or not -1 <= row["q"] <= 1 or not 0 <= row["prior_projected"] <= 1
                    or row["prior_search"] != row["prior_projected"]):
                raise ValueError("refill candidate value or probability differs")
        validate_attempts(dict(native, fair_sha=history.root_fair_sha), limits, require_depth_coverage=False)
    if not any(attempt["decisions"] > 2 or attempt["ticks"] > 60
            for shard in report["shards"] for attempt in shard["report"]["attempts"]):
        raise ValueError("refill suite lacks extended-depth coverage")
    return report


if __name__ == "__main__":
    if "--search" in sys.argv:
        result = validate_refill_search()
        print(json.dumps(dict(status="passed", roots=len(result["shards"]), simulations=768)))
    else:
        result = validate_refill_trajectory()
        print(json.dumps(dict(status="passed", roots=3, histories=6,
            changes=len(result["shards"][0]["report"]["changes"]))))
