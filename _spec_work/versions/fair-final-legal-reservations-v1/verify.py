import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
TRACE = ROOT.parents[1] / "evidence/fair-final-v1-20261019/native-refill-trajectory-1792489561748498000/project/input-trace.json"
ENGINE_SHA = "49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba"
SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(attempt, report, cleanup, trace):
    require(attempt["schema"] == "fair-final-legal-reservations-attempt.v1", "attempt schema")
    require(attempt["exit_code"] == 0 and attempt["training_admissible"] is False, "attempt status")
    require(attempt["engine_sha"] == ENGINE_SHA, "engine identity")
    require(report["schema"] == "fair-final-legal-reservations.v1" and report["status"] == "completed", "report status")
    require(report["training_admissible"] is False and report["mask_bypass"] is False, "diagnostic isolation")
    require(report["source_sha"] == SOURCE_SHA and report["model_unchanged"] is True, "frozen policy")
    require(report["trace_sha"] == attempt["payload_sha"]["project/input-trace.json"], "trace identity")
    require(all(attempt["payload_sha"]["project/" + name] == digest
        for name, digest in report["source_files"].items()), "report source identity")
    require(cleanup["exit_code"] == 0 and cleanup["error"] is None and cleanup["clean"] is True, "worker cleanup")
    require(cleanup["port"] == attempt["port"] and cleanup["process"]["phase"] == "stopped"
        and not cleanup["listening"] and not any(cleanup["pids_alive"].values()), "engine cleanup")
    require([record["tick"] for record in report["records"]] == [90, 110, 2598, 5602], "root coverage")
    groups = {group["tick"]: group for group in trace["boundaries"]}
    count, rejected, single_ready, kinds = 0, 0, 0, {}
    for record in report["records"]:
        tick = record["tick"]
        require(record["snapshot_released"] is True and record["root"]["tick"] == tick, "root restore")
        require(record["root"]["fair"] == groups[tick]["fair_sha"], "root FAIR identity")
        require(record["root"]["clock"]["ready"] == [0, 1]
            and not record["root"]["clock"]["pending"], "initial ready state")
        require(record["trials"], "empty trials")
        for trial in record["trials"]:
            repetitions = trial["repetitions"]
            require(len(repetitions) == 2 and repetitions[0] == repetitions[1]
                and trial["exact"] is True, "continuation restore mismatch")
            branch = repetitions[0]
            require(branch["selected_legal"] is True and branch["mask_bypass"] is False, "mask bypass")
            require(set(branch["actions"]) == {"0", "1"}, "simultaneous actions")
            reservations = {str(plan["owner"]): plan for plan in branch["reservations"]}
            require(set(reservations) == {"0", "1"}, "reservation owners")
            for owner, action in branch["actions"].items():
                plan = reservations[owner]
                require(plan["action"] == action and plan["created_tick"] == tick, "reservation identity")
                if action["card_id"] is None:
                    require(action["cell"] is None and action["delay_slot"] is None, "WAIT identity")
                    duration = 60
                else:
                    mask = record["masks"][owner]
                    shape = mask["shape"]
                    require(shape == [len(mask["card_ids"]), 576, 31], "legal mask dimensions")
                    values = bytes.fromhex(mask["plays_hex"])
                    require(len(values) == shape[0] * 576 * 31 and set(values) <= {0, 1}, "legal mask values")
                    require(action["card_id"] in mask["card_ids"] and type(action["cell"]) is int
                        and 0 <= action["cell"] < 576 and type(action["delay_slot"]) is int
                        and 1 <= action["delay_slot"] <= 30, "ACT coordinates")
                    card = mask["card_ids"].index(action["card_id"])
                    require(values[(card * 576 + action["cell"]) * 31 + action["delay_slot"]] == 1,
                        "selected ACT is illegal")
                    duration = 2 * action["delay_slot"]
                require(plan["due_tick"] == tick + duration, "reservation deadline")
            end = branch["final"]["tick"]
            require(1 <= end - tick <= 60, "decision horizon")
            require(end == min(plan["due_tick"] for plan in reservations.values()), "first complete boundary")
            require([step["tick"] for step in branch["steps"]] == list(range(tick + 1, end + 1)), "tick continuity")
            require(branch["final"]["clock"]["tick"] == end, "final clock")
            require(all(not step["outcomes"] for step in branch["steps"][:-1]), "early native outcome")
            outcomes = branch["steps"][-1]["outcomes"]
            due_acts = {str(plan["identity"]): plan for plan in reservations.values()
                if plan["due_tick"] == end and plan["action"]["card_id"] is not None}
            require(set(outcomes) == set(due_acts)
                and all(value in ("executed", "rejected") for value in outcomes.values()), "native settlement completeness")
            public = "executed" in outcomes.values()
            expected_evidence, expected_pending = {}, []
            for owner, plan in reservations.items():
                if plan["due_tick"] == end:
                    expected_evidence[owner] = ("wait_expired" if plan["action"]["card_id"] is None
                        else outcomes[str(plan["identity"])])
                elif public:
                    expected_evidence[owner] = "preempted"
                else:
                    expected_pending.append(plan)
            evidence = branch["evidence"]
            require(evidence and all(row["reason"] in ("executed", "rejected", "wait_expired", "preempted")
                for row in evidence), "settlement evidence")
            require(len(evidence) == len(expected_evidence)
                and {str(row["owner"]): row["reason"] for row in evidence} == expected_evidence, "complete decision evidence")
            require(branch["final"]["clock"]["ready"] == sorted(int(owner) for owner in expected_evidence)
                and sorted(branch["final"]["clock"]["pending"], key=lambda plan: plan["owner"])
                == sorted(expected_pending, key=lambda plan: plan["owner"]), "ready and pending transition")
            for row in evidence:
                plan = reservations[str(row["owner"])]
                require(row["reservation"] == plan["identity"] and row["tick"] == end, "settlement identity")
                if row["reason"] != "preempted":
                    require(plan["due_tick"] == end, "due-before-observe settlement")
                else:
                    require(plan["due_tick"] > end, "premature preemption classification")
            is_rejected = any(row["reason"] == "rejected" for row in evidence)
            is_single = len(branch["final"]["clock"]["ready"]) == 1
            require(branch["rejected"] == is_rejected and branch["single_ready"] == is_single, "branch classification")
            require(branch["final"]["rng"] == record["root"]["rng"], "unexpected execution RNG use")
            count += 1
            rejected += int(is_rejected)
            single_ready += int(is_single)
            kinds[trial["kind"]] = kinds.get(trial["kind"], 0) + 1
    return dict(status="passed_limited_scope", trials=count, continuations=count * 2,
        rejected_trials=rejected, single_ready_trials=single_ready, trial_kinds=kinds,
        training_admissible=False, general_pending_gate_closed=False,
        limitation="legal sampled continuations on one archived trajectory; no global unreachability proof")


def main():
    directory = Path(sys.argv[1])
    attempt, report, cleanup = [json.loads((directory / name).read_text())
        for name in ("attempt.json", "report.json", "cleanup.json")]
    require(hashlib.sha256(TRACE.read_bytes()).hexdigest() == report["trace_sha"], "fixed trace differs")
    result = verify(attempt, report, cleanup, json.loads(TRACE.read_text()))
    result["verifier_sha"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["artifact_sha"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("attempt.json", "report.json", "cleanup.json")}
    (directory / "verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
