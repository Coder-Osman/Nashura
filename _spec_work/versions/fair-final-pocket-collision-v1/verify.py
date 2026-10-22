import hashlib
import json
from pathlib import Path
import sys


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
ENGINE_SHA = "49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(attempt, report, cleanup):
    require(attempt["schema"] == "fair-final-pocket-collision-attempt.v1"
        and attempt["exit_code"] == 0 and attempt["training_admissible"] is False, "attempt status")
    require(attempt["engine_sha"] == ENGINE_SHA and report["source_sha"] == SOURCE_SHA, "model or engine identity")
    require(report["schema"] == "fair-final-pocket-collision.v1"
        and report["status"] == "collision-tested", "no collision root reached")
    require(report["training_admissible"] is False and report["mask_bypass"] is False
        and report["model_unchanged"] is True, "diagnostic isolation")
    require(report["seed"] == 78101907 and report["execution_seed"] == 78101947, "registered seeds")
    require(all(attempt["payload_sha"]["project/" + name] == digest
        for name, digest in report["source_files"].items()), "source provenance")
    require(cleanup["exit_code"] == 0 and cleanup["error"] is None and cleanup["clean"] is True
        and cleanup["process"]["phase"] == "stopped" and not any(cleanup["pids_alive"].values())
        and cleanup["listening"] is False and cleanup["port"] == attempt["port"], "engine cleanup")
    require(report["pocket_open"] is True and report["ended"] is False and report["truncated"] is False,
        "live post-tower-loss root")
    require(report["snapshot_released"] is True and report["records"], "snapshot lifecycle")
    trajectory = report["trajectory"]
    require(trajectory and trajectory[0]["tick"] == 90
        and all(row["selected_legal"] is True and set(row["fair_sha"]) == set(row["actions"])
            for row in trajectory), "setup legal trajectory")
    require(all(left["tick"] < right["tick"] for left, right in zip(trajectory, trajectory[1:])), "trajectory order")
    require(any(tower["owner"] == 1 and tower["kind"] != "king"
        and (tower["hp"] == 0 or tower["active"] is False)
        for tower in report["root_public_towers"]), "public destroyed tower evidence")
    root = report["root"]
    require(root["tick"] == report["tick"] and root["clock"]["tick"] == root["tick"]
        and root["clock"]["ready"] == [0, 1] and not root["clock"]["pending"]
        and set(root["fair"]) == {"0", "1"}, "simultaneous root")
    replay = report["replay"]
    require(replay["status"] == "passed" and replay["boundaries"] == len(trajectory)
        and replay["public_towers"] == report["root_public_towers"]
        and replay["native_digest_compared"] is False
        and all(replay["root"][key] == root[key] for key in ("tick", "clock", "fair", "rng")),
        "fresh reset trajectory replay")
    rejected = single_ready = 0
    delays = []
    for trial in report["records"]:
        repetitions = trial["repetitions"]
        require(trial["exact"] is True and len(repetitions) == 2 and repetitions[0] == repetitions[1], "repeat mismatch")
        branch = repetitions[0]
        require(branch["selected_legal"] is True and branch["mask_bypass"] is False, "branch mask bypass")
        require(set(branch["actions"]) == {"0", "1"}, "branch actors")
        delay = trial["delay"]
        require(delay in (1, 30), "diagnostic delay")
        delays.append(delay)
        native_cells = []
        for owner, action in branch["actions"].items():
            require(action["card_id"] == 27000000 and action["delay_slot"] == delay
                and type(action["cell"]) is int and 0 <= action["cell"] < 576, "collision action identity")
            mask = report["masks"][owner]
            require(mask["shape"] == [len(mask["card_ids"]), 576, 31], "mask dimensions")
            values = bytes.fromhex(mask["plays_hex"])
            require(len(values) == len(mask["card_ids"]) * 576 * 31 and set(values) <= {0, 1}, "mask values")
            index = mask["card_ids"].index(27000000)
            require(values[(index * 576 + action["cell"]) * 31 + delay] == 1, "illegal selected action")
            cell = action["cell"] if owner == "0" else 575 - action["cell"]
            native_cells.append((cell % 18, cell // 18))
        require(all(abs(native_cells[0][axis] - native_cells[1][axis]) < 3 for axis in (0, 1)), "no positive footprint overlap")
        reservations = {str(plan["owner"]): plan for plan in branch["reservations"]}
        end = root["tick"] + 2 * delay
        require(set(reservations) == {"0", "1"} and all(plan["created_tick"] == root["tick"]
            and plan["due_tick"] == end and plan["action"] == branch["actions"][owner]
            for owner, plan in reservations.items()), "reservation deadlines")
        require([row["tick"] for row in branch["steps"]] == list(range(root["tick"] + 1, end + 1))
            and all(not row["outcomes"] for row in branch["steps"][:-1]), "complete boundary timing")
        outcomes = branch["steps"][-1]["outcomes"]
        require(set(outcomes) == {str(plan["identity"]) for plan in reservations.values()}
            and all(status in ("executed", "rejected") for status in outcomes.values()), "native outcomes")
        evidence = branch["evidence"]
        require(len(evidence) == 2 and {str(row["owner"]) for row in evidence} == {"0", "1"}, "settlement evidence")
        for row in evidence:
            plan = reservations[str(row["owner"])]
            require(row["tick"] == end and row["reservation"] == plan["identity"]
                and row["reason"] == outcomes[str(plan["identity"])], "settlement mapping")
        require(branch["final"]["tick"] == end and branch["final"]["clock"]["tick"] == end
            and branch["final"]["clock"]["ready"] == [0, 1] and not branch["final"]["clock"]["pending"], "final boundary")
        require(branch["final"]["rng"] == root["rng"], "unexpected RNG use")
        is_rejected = "rejected" in outcomes.values()
        require(branch["rejected"] == is_rejected and branch["single_ready"] is False, "branch classification")
        rejected += int(is_rejected)
        single_ready += int(branch["single_ready"])
    require(delays == [1, 30], "short and long reservation coverage")
    return dict(status="passed_limited_scope", trials=len(delays), continuations=len(delays) * 2,
        rejected_trials=rejected, single_ready_trials=single_ready, training_admissible=False,
        general_pending_gate_closed=False,
        limitation="one legal post-tower-loss trajectory; no general rejection or belief reconstruction proof")


def main():
    directory = Path(sys.argv[1])
    payload = [json.loads((directory / name).read_text()) for name in ("attempt.json", "report.json", "cleanup.json")]
    result = verify(*payload)
    result["verifier_sha"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["artifact_sha"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("attempt.json", "report.json", "cleanup.json")}
    (directory / "verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
