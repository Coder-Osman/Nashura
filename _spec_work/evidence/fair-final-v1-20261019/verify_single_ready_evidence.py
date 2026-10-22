import hashlib
import json
from pathlib import Path
import sys


def validate_single_ready_evidence(root, workspace):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from native_runner.training.fair_final.policy import FullAction
    from native_runner.training.fair_final.timing import DecisionClock, Reservation

    report = json.loads((root / "native-single-ready-report.json").read_text())
    if (report["schema"] != "fair-final-native-single-ready-diagnostic.v1"
            or report["status"] != "passed" or report["training_admissible"] is not False
            or report["published"] is not False or report["normal_training_single_ready_verified"] is not False
            or report["wake_protocol"] != "public-deployment-own-deadline.v1"
            or report["model_unchanged"] is not True
            or report["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"):
        raise ValueError("single-ready diagnostic identity or isolation differs")
    if hashlib.sha256((root / "run_single_ready_validation.py").read_bytes()).hexdigest() != report["runner_sha256"]:
        raise ValueError("single-ready runner identity differs")
    if not report["source_sha256"] or not report["artifacts"]:
        raise ValueError("missing single-ready provenance")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("single-ready source changed")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("single-ready artifact changed")

    def artifact(name):
        paths = [root / relative for relative in report["artifacts"] if Path(relative).name == name]
        if len(paths) != 1:
            raise ValueError("missing unique single-ready artifact")
        return json.loads(paths[0].read_text())

    if artifact("report.json") != {key: value for key, value in report.items()
            if key not in ("runner_sha256", "artifacts", "cleanup")}:
        raise ValueError("single-ready raw report differs")
    if artifact("probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("single-ready deployment identity differs")
    modes = {"staggered_wait", "early_public_act", "same_tick_public_act"}
    attempt = artifact("attempt.json")
    if (attempt["exit_code"] != 0 or attempt["source_sha256"] != report["source_sha256"]
            or attempt["seats"] != [0, 1] or set(attempt["modes"]) != modes):
        raise ValueError("single-ready registered coverage differs")
    preflight = artifact("preflight.json")
    if (preflight["memory_available"] < 32 * 1024**3 or preflight["free_bytes"] < 5 * 1024**3
            or preflight["port"] != 29630
            or preflight["engine_sha256"] != "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"):
        raise ValueError("single-ready preflight identity differs")
    records = report["records"]
    if len(records) != 2 or {entry["owner"] for entry in records} != {0, 1}:
        raise ValueError("single-ready both-seat coverage missing")
    for entry in records:
        owner, start = entry["owner"], entry["start_tick"]
        opponent = 1 - owner
        root_state = entry["root"]
        clock = DecisionClock.restore(root_state["clock"])
        rejected = entry["rejected_reservation"]
        Reservation(**{**rejected, "action": FullAction(**rejected["action"])})
        if (entry["purpose"] != "native-rejection-diagnostic"
                or entry["training_admissible"] is not False or entry["published"] is not False
                or entry["mask_bypass"] is not True or entry["payment"]["legal"] is not False
                or entry["payment"]["payable_100ms"] is not False
                or entry["payment"]["elixir_raw"] >= entry["payment"]["cost_raw"]
                or entry["snapshot_released"] is not True
                or rejected["owner"] != owner or rejected["created_tick"] != start
                or rejected["due_tick"] != start + 2
                or rejected["action"] != entry["injected_illegal_action"]
                or clock.tick != start + 2 or root_state["tick"] != clock.tick
                or clock.ready != {owner} or set(clock.pending) != {opponent}
                or set(root_state["information"]) != {str(owner)}
                or clock.pending[opponent].created_tick != start
                or clock.pending[opponent].due_tick != start + 60
                or clock.pending[opponent].action.card_id is not None
                or clock.evidence[-1] != dict(reservation=rejected["identity"], owner=owner, tick=start + 2, reason="rejected")
                or not any(event["event_type"] == "action_rejected" and event["owner"] == owner
                    and event["tick"] == start + 2 for event in entry["native_outcomes"])):
            raise ValueError("single-ready rejection or pending-root evidence differs")
        if len(entry["branches"]) != 3 or {branch["mode"] for branch in entry["branches"]} != modes:
            raise ValueError("single-ready branch coverage missing")
        for branch in entry["branches"]:
            replays = branch["replays"]
            if branch["exact"] is not True or len(replays) != 2 or replays[0] != replays[1]:
                raise ValueError("single-ready restored branches diverged")
            replay = replays[0]
            mode = branch["mode"]
            selected = FullAction(**replay["selected"])
            preserved = replay["preserved"]
            if (replay["mode"] != mode or replay["selected_legal"] is not True
                    or replay["opponent_rng_unchanged"] is not True
                    or preserved != root_state["clock"]["pending"][0]
                    or replay["final"]["rng"][str(opponent)] != root_state["rng"][str(opponent)]
                    or replay["final"]["rng"][str(owner)] == root_state["rng"][str(owner)]):
                raise ValueError("single-ready reservation or RNG differs")
            target = start + 4 if mode == "early_public_act" else start + 60
            if [step["tick"] for step in replay["trace"]] != list(range(start + 3, target + 1)):
                raise ValueError("single-ready diagnostic skipped ticks")
            for step in replay["trace"][:-1]:
                if step["ready"] or preserved not in step["pending"]:
                    raise ValueError("opponent reservation changed before deadline")
            final = DecisionClock.restore(replay["final"]["clock"])
            lifecycle = final.evidence[len(clock.evidence):]
            expected = {owner: "wait_expired" if mode == "staggered_wait" else "executed",
                opponent: "preempted" if mode == "early_public_act" else "wait_expired"}
            if len(lifecycle) != 2 or {event["owner"]: event["reason"] for event in lifecycle} != expected:
                raise ValueError("single-ready due-before-observe lifecycle differs")
            if final.evidence[:len(clock.evidence)] != clock.evidence:
                raise ValueError("single-ready earlier lifecycle changed")
            for event in lifecycle:
                expected_identity = preserved["identity"] if event["owner"] == opponent else clock.next_identity
                expected_tick = start + 62 if mode == "staggered_wait" and event["owner"] == owner else target
                if event["reservation"] != expected_identity or event["tick"] != expected_tick:
                    raise ValueError("single-ready lifecycle reservation or timestamp differs")
            if mode == "staggered_wait":
                if (selected.card_id is not None or final.tick != start + 62 or final.ready != {owner}
                        or set(final.pending) != {opponent} or final.pending[opponent].due_tick != start + 120
                        or len(replay["boundaries"]) != 2
                        or replay["boundaries"][0]["clock"]["ready"] != [opponent]
                        or replay["boundaries"][0]["tick"] != start + 60):
                    raise ValueError("staggered WAIT boundary differs")
            elif (selected.card_id is None or selected.delay_slot != (1 if mode == "early_public_act" else 29)
                    or final.tick != target or final.ready != {0, 1} or final.pending
                    or len(replay["boundaries"]) != 1
                    or not any(event["event_type"] == "action_executed" and event["owner"] == owner
                        for event in replay["trace"][-1]["events"])):
                raise ValueError("public ACT boundary differs")
            for boundary in replay["boundaries"]:
                boundary_clock = DecisionClock.restore(boundary["clock"])
                if (boundary["tick"] != boundary_clock.tick
                        or set(boundary["information"]) != {str(seat) for seat in boundary_clock.ready}):
                    raise ValueError("single-ready frozen information differs from boundary")
    cleanup = report["cleanup"]
    if (artifact("cleanup.json") != cleanup or cleanup["exit_code"] != 0 or cleanup["port"] != 29630
            or cleanup["listening"] or cleanup["process"]["phase"] != "stopped"
            or set(cleanup["pids_alive"]) != {"pid", "child_pid"} or any(cleanup["pids_alive"].values())):
        raise ValueError("single-ready engine cleanup incomplete")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    report = validate_single_ready_evidence(root, root.parents[2])
    print(json.dumps(dict(status="passed", seats=len(report["records"]), branches=6, training_admissible=False)))
