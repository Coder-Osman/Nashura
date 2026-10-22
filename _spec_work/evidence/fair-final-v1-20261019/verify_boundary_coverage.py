from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2]/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.contracts import EventV1
from native_runner.training.fair_final.episode import terminal_result
from native_runner.training.fair_final.native import FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_final.visibility import event_visible_to_owner
from native_runner.training.fair_search.contracts import require_sha
from run_boundary_coverage_validation import JOBS
from verify_local_reconditioning import validate_phase_engine_build
from tools.probe_fair_final_boundary_coverage import SOURCE_SHA


def validate_closure(report=None, *, root=ROOT):
    if report is None:
        report = json.loads((root/"boundary-closure-report.json").read_text())
    expected_sources = ("native_runner/training/fair_final/timing.py",
        "native_runner/training/fair_final/native.py", "native_runner/training/fair_final/policy.py")
    identities = {name: hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() for name in expected_sources}
    histogram = Counter()
    durations = [60, *range(2, 61, 2)]
    for first in durations:
        for second in durations:
            for public in (61, *range(1, 61)):
                histogram[str(min(first, second, public))] += 1
    expected = dict(schema="fair-final-boundary-closure.v1", status="passed", training_admissible=False,
        wake_protocol=FinalDecisionRuntime.wake_protocol, cases=58621, action_pairs=961,
        public_wake_schedules=61, duration_histogram=dict(histogram),
        assumptions=["both-ready-opening", "atomic-both-owner-submission", "WAIT-60-ticks",
            "ACT-even-2-through-60-ticks", "successful-ACT-publicly-wakes-both", "no-rejected-or-unconfirmed-action"],
        counterexamples=dict(rejection_single_ready=True, nonpublic_success_single_ready=True),
        conclusion="every-nonterminal-reachable-decision-group-is-both-ready-under-listed-assumptions",
        exclusions=["native-event-publicity-without-evidence", "general-pending-world-conditioning",
            "other-wake-protocol", "search-branches-with-rejections", "training-admission"],
        source_sha256=identities,
        enumerator_sha256=hashlib.sha256((root/"boundary_reachability.py").read_bytes()).hexdigest())
    if report != expected:
        raise ValueError("boundary closure assumptions, coverage or source identity differs")
    return report


def validate_trace(trace, native):
    if trace["schema"] != "fair-final-boundary-trajectory.v1":
        raise ValueError("boundary trace identity differs")
    clock = DecisionClock()
    clock.begin_tick(trace["opening_tick"], wake_owners=(0,1), adjudicate_due=lambda *unused:{},
        freeze=lambda *unused:None)
    boundary_index = 0
    groups = trace["boundaries"]
    previous_tick = trace["opening_tick"]
    executed_count = incomplete_groups = 0
    for offset, row in enumerate(trace["ticks"]):
        if type(row["tick"]) is not int or row["tick"] != previous_tick+1:
            raise ValueError("boundary trace skipped a native tick")
        if clock.ready:
            if boundary_index >= len(groups):
                raise ValueError("boundary submission missing")
            group = groups[boundary_index]
            if (clock.ready != {0,1} or clock.pending or group["ready"] != [0,1] or group["pending"] != []
                    or group["tick"] != clock.tick or group["legal"] is not True
                    or set(group["actions"]) != {"0","1"} or set(group["fair_sha"]) != {"0","1"}):
                raise ValueError("normal simultaneous boundary or legality differs")
            for sha in group["fair_sha"].values():
                require_sha(sha)
            draws = group["draw_sizes"]
            if (len(draws) != 2 or {entry["owner"] for entry in draws} != {0,1}
                    or any(type(entry[key]) is not int or not 0 <= entry[key] <= 8
                        for entry in draws for key in ("hand","cycle"))):
                raise ValueError("boundary draw layout evidence differs")
            incomplete_groups += int(any(entry["hand"] != 4 or entry["cycle"] != 4 for entry in draws))
            selected = {int(owner):FullAction(**action) for owner,action in group["actions"].items()}
            if native["mode"] == "wait" and any(action.card_id is not None for action in selected.values()):
                raise ValueError("WAIT diagnostic played a card")
            clock.commit(selected, legal=lambda *unused:True)
            boundary_index += 1
        outcomes = {int(key):value for key,value in row["outcomes"].items()}
        due = [plan for plan in clock.pending.values() if plan.due_tick == row["tick"] and plan.action.card_id is not None]
        if set(outcomes) != {plan.identity for plan in due} or any(reason != "executed" for reason in outcomes.values()):
            raise ValueError("normal trajectory has a rejected or unconfirmed ACT")
        events = [EventV1.from_mapping(event) for event in row["public_executions"]]
        if (any(event.tick != row["tick"] or event.event_type != "action_executed"
                or event.data.get("private_to") is not None
                or not all(event_visible_to_owner(event, owner) for owner in (0,1)) for event in events)
                or Counter((event.owner,event.card_id) for event in events) !=
                    Counter((plan.owner,plan.action.card_id) for plan in due)):
            raise ValueError("successful ACT public-event evidence differs")
        executed_count += len(events)
        if (type(row["ended"]) is not bool or type(row["truncated"]) is not bool
                or type(row["actions_open"]) is not bool or row["truncated"]
                or row["ended"] != (offset == len(trace["ticks"])-1)):
            raise ValueError("boundary trajectory terminal or truncation differs")
        evidence_offset = len(clock.evidence)
        if row["ended"] or not row["actions_open"]:
            clock.finish(row["tick"], outcomes=outcomes)
        else:
            clock.begin_tick(row["tick"], wake_owners=(0,1) if events else (),
                adjudicate_due=lambda *unused:outcomes, freeze=lambda *unused:None)
        if row["ready"] != sorted(clock.ready) or row["evidence"] != clock.evidence[evidence_offset:]:
            raise ValueError("native boundary differs from independent clock replay")
        previous_tick = row["tick"]
    if (not trace["ticks"] or boundary_index != len(groups) or clock.capture() != trace["final_clock"]
            or clock.pending or clock.ready or not groups or clock.next_identity != 2*len(groups)):
        raise ValueError("boundary trace incomplete or controller not settled")
    result = trace["result"]
    winner = result["winner"]
    labels = (0.,0.) if winner is None else ((1.,-1.) if winner == 0 else (-1.,1.))
    terminal = SimpleNamespace(ended=True, winner=winner, result_by_owner=labels, terminal_tick=clock.tick)
    expected = terminal_result(terminal,result["native_evidence"],truncated=False,failed=False)
    if (result != expected or native["result"] != result or native["opening_tick"] != trace["opening_tick"]
            or native["terminal_tick"] != clock.tick or native["boundary_groups"] != len(groups)
            or native["decisions"] != 2*len(groups) or native["single_ready_groups"] != 0
            or native["incomplete_draw_groups"] != incomplete_groups
            or native["lifecycle"] != dict(Counter(event["reason"] for event in clock.evidence))):
        raise ValueError("boundary totals or native terminal label differs")
    if (native["mode"] == "wait" and (winner is not None or executed_count != 0)
            or native["mode"] == "sampled" and executed_count == 0):
        raise ValueError("normal play and real draw coverage missing")
    return dict(boundaries=len(groups), decisions=2*len(groups), ticks=len(trace["ticks"]),
        public_executions=executed_count, incomplete_draw_groups=incomplete_groups)


def validate_boundary_coverage(report=None, *, root=ROOT):
    if report is None:
        report=json.loads((root/"native-boundary-coverage-report.json").read_text())
    if (report["schema"] != "fair-final-boundary-coverage-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["jobs"] != JOBS
            or report["purpose"] != "complete-trajectory-boundary-reachability"):
        raise ValueError("boundary suite scope differs")
    for filename,key in (("run_boundary_coverage_validation.py","runner_sha256"),
            ("boundary_coverage_worker.py","worker_sha256")):
        if hashlib.sha256((root/filename).read_bytes()).hexdigest() != report[key]:
            raise ValueError("boundary runner identity differs")
    if not report["artifacts"] or not report["source_sha256"]:
        raise ValueError("boundary manifest empty")
    for mapping,base in ((report["artifacts"],root),(report["source_sha256"],SOURCE)):
        for relative,expected in mapping.items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("boundary evidence path escapes root")
            if hashlib.sha256((base/relative).read_bytes()).hexdigest()!=expected:
                raise ValueError("boundary source or artifact changed")

    def artifact_path(suffix):
        paths=[root/relative for relative in report["artifacts"] if relative.endswith("/"+suffix)]
        if len(paths)!=1:
            raise ValueError("boundary artifact missing or ambiguous")
        return paths[0]

    def artifact(suffix):
        return json.loads(artifact_path(suffix).read_text())

    engine_sha=validate_phase_engine_build(artifact("engine-build-manifest.json"))
    attempt,registration,preflight=artifact("attempt.json"),artifact("run-manifest.json"),artifact("preflight.json")
    if (attempt["exit_code"] != 0 or registration["jobs"] != JOBS or registration["root"] != report["remote"]
            or any(attempt[key] != report[key] for key in ("source_sha256","remote","jobs","purpose",
                "worker_sha256","runner_sha256","engine_sha256"))
            or any(value!=engine_sha for value in (report["engine_sha256"],registration["engine_sha256"],preflight["engine_sha256"]))
            or preflight["memory_available"]<64*1024**3 or preflight["free_bytes"]<5*1024**3
            or artifact("project/probe-source-manifest.json")!=report["source_sha256"]
            or hashlib.sha256(artifact_path("project/run.py").read_bytes()).hexdigest()!=report["worker_sha256"]):
        raise ValueError("boundary deployment evidence differs")
    launcher_sha=hashlib.sha256((root/"phase_snapshot_launcher.py").read_bytes()).hexdigest()
    if hashlib.sha256(artifact_path("project/phase_snapshot_launcher.py").read_bytes()).hexdigest()!=launcher_sha:
        raise ValueError("boundary launcher identity differs")
    results=artifact("run-results.json")
    if ([entry["job"] for entry in results]!=JOBS or [entry["job"] for entry in report["shards"]]!=JOBS
            or any(entry["exit_code"]!=0 or entry["error"] is not None or entry["clean"] is not True for entry in results)):
        raise ValueError("boundary job coverage incomplete")
    children=set()
    for shard in report["shards"]:
        job,native,cleanup=shard["job"],shard["report"],shard["cleanup"]
        if (artifact(job["name"]+"/report.json")!=native
                or native["schema"]!="fair-final-native-boundary-coverage.v1" or native["status"]!="passed"
                or native["training_admissible"] is not False or native["model_unchanged"] is not True
                or native["mode"]!=job["mode"] or native["source_sha256"]!=report["source_sha256"]
                or native["checkpoint_sha256"]!=SOURCE_SHA or native["wake_protocol"]!=FinalDecisionRuntime.wake_protocol
                or native["exclusions"]!=["arbitrary-wake-protocol", "rejected-action-search-roots", "full-game-search", "training-admission"]):
            raise ValueError("boundary native report identity differs")
        private=artifact(job["name"]+"/private-inputs.json")
        if private!=dict(engine_seed=job["engine_seed"],execution_seed=78101946):
            raise ValueError("boundary diagnostic seed identity differs")
        trace_path=artifact_path(job["name"]+"/trace.json")
        if hashlib.sha256(trace_path.read_bytes()).hexdigest()!=native["trace_sha256"]:
            raise ValueError("boundary trace digest differs")
        validate_trace(json.loads(trace_path.read_text()),native)
        if (artifact(job["name"]+"/cleanup.json")!=cleanup or cleanup["exit_code"]!=0
                or cleanup["error"] is not None or cleanup["clean"] is not True
                or [engine["port"] for engine in cleanup["engines"]]!=job["ports"]):
            raise ValueError("boundary cleanup incomplete")
        for engine in cleanup["engines"]:
            process=engine["process"]
            identity=process["phase_snapshot_engine"]
            if (engine["listening"] or any(engine["pids_alive"].values()) or process["phase"]!="stopped"
                    or identity["sha256"]!=engine_sha or identity["launcher_sha256"]!=launcher_sha
                    or identity["path"]!=registration["engine_path"]
                    or artifact(job["name"]+f"/engine-{engine['port']}/process.json")!=process):
                raise ValueError("boundary engine identity or release differs")
            children.add(process["child_pid"])
    if len(children)!=2:
        raise ValueError("boundary diagnostics shared an engine")
    return report


if __name__=="__main__":
    closure = validate_closure()
    result=validate_boundary_coverage()
    print(json.dumps(dict(status="passed",games=len(result["shards"]),
        timing_cases=closure["cases"],
        boundaries=sum(shard["report"]["boundary_groups"] for shard in result["shards"]))))
