from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from verify_refill import ROOT, validate_refill_search, validate_refill_trajectory


TRAJECTORY_CASES = ("missing_root", "missing_change", "wrong_fifo", "wrong_terminal", "missing_repeat",
    "changed_repeat", "false_restore", "skipped_tick", "wrong_input", "changed_source",
    "changed_artifact", "old_engine", "unclean_engine", "training_claim", "wrong_history")
SEARCH_CASES = ("missing_shard", "wrong_budget", "wrong_target", "wrong_action", "wrong_perspective",
    "short_horizon", "wrong_seed", "wrong_hole", "wrong_fifo", "copied_actual", "missing_attempt",
    "missing_restore", "changed_continuation", "changed_source", "unclean_engine", "training_claim",
    "missing_conditioning", "invalid_conditioning_mass", "unbounded_conditioning",
    "late_missing_attempt", "late_broken_chain", "missing_suite_depth")


def main(search=False):
    validator = validate_refill_search if search else validate_refill_trajectory
    baseline = validator()
    cases = SEARCH_CASES if search else TRAJECTORY_CASES
    name = "refill-search" if search else "refill-trajectory"
    rejected = []
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="fair-refill-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for filename in (("run_refill_search_validation.py", "refill_search_worker.py", "phase_snapshot_launcher.py")
                    if search else ("run_refill_validation.py", "refill_worker.py", "phase_snapshot_launcher.py")):
                (temporary/filename).symlink_to(ROOT/filename)

            def replace(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                destination = temporary/relative
                destination.unlink()
                destination.write_text(json.dumps(payload))
                report["artifacts"][relative] = hashlib.sha256(destination.read_bytes()).hexdigest()

            shard = report["shards"][4 if case.startswith("late_") else 0]
            native = shard["report"]
            prefix = shard["job"]["name"]+"/"
            if case == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
            elif case == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "unclean_engine":
                shard["cleanup"]["engines"][0]["listening"] = True
                replace(prefix+"cleanup.json", shard["cleanup"])
            elif case == "training_claim":
                native["training_admissible"] = True
            elif search:
                result = native["outcome"]["result"]
                if case == "missing_shard":
                    report["shards"].pop()
                elif case == "wrong_budget":
                    result["candidates"][0]["visits"] += 1
                elif case == "wrong_target":
                    native["outcome"]["target"][0][1] += 1
                elif case == "wrong_action":
                    native["outcome"]["selected"] = "invalid"
                elif case == "wrong_perspective":
                    result["value_perspective"] = 1-native["owner"]
                elif case == "short_horizon":
                    native["limits"]["soft_decisions"] = 2
                elif case == "wrong_seed":
                    native["audit"]["world_manifest"]["conditioning"]["seed_commitment"] = "0"*64
                elif case == "wrong_hole":
                    native["audit"]["world_manifest"]["conditioning"]["refill_holes"] = [0, 1, 2, 3]
                elif case == "wrong_fifo":
                    row = native["search_layouts"][1]
                    row["cycle"][0], row["cycle"][1] = row["cycle"][1], row["cycle"][0]
                elif case == "copied_actual":
                    native["audit"]["world_manifest"]["actual_state_copied"] = True
                elif case == "missing_attempt":
                    native["attempts"].pop()
                elif case == "late_missing_attempt":
                    native["attempts"].pop()
                elif case == "late_broken_chain":
                    native["attempts"][0]["transitions"][0]["before_sha"] = "0"*64
                elif case == "missing_suite_depth":
                    for changed in report["shards"]:
                        entry = changed["report"]
                        edge = next(edge for attempt in entry["attempts"] for edge in attempt["transitions"]
                            if edge["completed"] and edge["terminal"] is None
                            and edge["before_tick"] == entry["tick"] and edge["ticks"] <= 60
                            and edge["decisions"] <= 2)
                        entry["attempts"] = [dict(ticks=edge["ticks"], decisions=edge["decisions"],
                            transitions=[deepcopy(edge)]) for index in range(128)]
                        entry["audit"].update(attempts=128, discarded_edges=0,
                            completed_lifecycle={edge["reason"]: 128})
                        entry["outcome"]["result"]["invalid_attempts"] = 0
                        replace(changed["job"]["name"]+"/report.json", entry)
                elif case == "missing_restore":
                    native["world_lifecycle"]["restores"] -= 1
                elif case == "changed_continuation":
                    native["reconstruction"]["continuation"][1]["digest"] = "0"*64
                    replace(prefix+"reconstruction.json", native["reconstruction"])
                elif case == "missing_conditioning":
                    native["audit"]["world_manifest"]["conditioning"].pop("hidden_plan_conditioning")
                elif case == "invalid_conditioning_mass":
                    native["audit"]["world_manifest"]["conditioning"]["hidden_plan_conditioning"].update(
                        calls=1, minimum_log_mass=1.)
                elif case == "unbounded_conditioning":
                    native["audit"]["world_manifest"]["conditioning"]["hidden_plan_conditioning"].update(
                        calls=1, maximum_prefixes=5000)
            else:
                if case == "missing_root":
                    native["roots"].pop()
                elif case == "missing_change":
                    native["changes"] = [row for row in native["changes"] if row["tick"] != 92]
                elif case == "wrong_fifo":
                    player = native["changes"][1]["players"][0]
                    player["cycle"][0]["cycleIndex"], player["cycle"][1]["cycleIndex"] = 1, 0
                elif case == "wrong_terminal":
                    native["result"]["winner"] = None
                elif case == "missing_repeat":
                    native["roots"][0]["continuations"][1].pop()
                elif case == "changed_repeat":
                    native["roots"][0]["continuations"][1][0]["digest"] = "0"*64
                elif case == "false_restore":
                    native["roots"][0]["restored"] = False
                elif case == "skipped_tick":
                    for continuation in native["roots"][0]["continuations"]:
                        continuation[0]["tick"] += 1
                elif case == "wrong_input":
                    native["input_trace_sha256"] = "0"*64
                elif case == "old_engine":
                    report["engine_sha256"] = "0"*64
                elif case == "wrong_history":
                    suffix = "trajectory/history-110-0.json"
                    relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                    history = json.loads((ROOT/relative).read_text())
                    history["root_fair_sha"] = "0"*64
                    replace(suffix, history)
            replace(prefix+"report.json", native)
            try:
                validator(report, root=temporary)
            except ValueError as error:
                if case == "missing_suite_depth" and str(error) != "refill suite lacks extended-depth coverage":
                    raise AssertionError("depth coverage mutation failed before suite check") from error
                rejected.append(case)
            else:
                raise AssertionError("accepted altered refill evidence: "+case)
    result = dict(status="passed", training_admissible=False, rejected=rejected, count=len(rejected),
        validator_sha256=hashlib.sha256((ROOT/"verify_refill.py").read_bytes()).hexdigest(),
        test_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        baseline_report_sha256=hashlib.sha256((ROOT/("native-"+name+"-report.json")).read_bytes()).hexdigest())
    if search:
        result["attempt_validator_sha256"] = hashlib.sha256((ROOT/"verify_prior_search.py").read_bytes()).hexdigest()
    (ROOT/(name+"-validator-tests.json")).write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main(search="--search" in sys.argv)
