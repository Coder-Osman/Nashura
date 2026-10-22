from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_boundary_coverage import ROOT, validate_boundary_coverage, validate_closure


CASES = ("missing_shard", "missing_tick", "single_ready", "pending_root", "illegal_action",
    "missing_public_event", "private_event", "wrong_public_card", "rejection", "missing_outcome",
    "invalid_delay", "wrong_ready", "wrong_lifecycle", "truncation", "early_terminal",
    "false_draw", "wrong_label", "wrong_draw_count", "invalid_draw_size", "changed_source",
    "changed_artifact", "old_engine", "unclean_engine", "training_claim")
CLOSURE_CASES = ("missing_assumption", "missing_exclusion", "wrong_cases", "wrong_histogram",
    "wrong_source", "wrong_enumerator", "missing_counterexample", "training_claim")


def main():
    baseline = validate_boundary_coverage()
    closure = validate_closure()
    rejected = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-boundary-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_boundary_coverage_validation.py", "boundary_coverage_worker.py",
                    "phase_snapshot_launcher.py"):
                (temporary/name).symlink_to(ROOT/name)

            def replace_artifact(suffix, payload):
                relative = next(name for name in report["artifacts"] if name.endswith("/"+suffix))
                target = temporary/relative
                target.unlink()
                target.write_text(json.dumps(payload))
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                report["artifacts"][relative] = digest
                return digest

            shard = report["shards"][0]
            native = shard["report"]
            prefix = shard["job"]["name"]+"/"
            relative = next(name for name in report["artifacts"] if name.endswith("/"+prefix+"trace.json"))
            trace = json.loads((ROOT/relative).read_text())
            executed = next(row for row in trace["ticks"] if row["public_executions"])
            if case == "missing_shard":
                report["shards"].pop()
            elif case == "missing_tick":
                trace["ticks"].pop(5)
            elif case == "single_ready":
                trace["boundaries"][0]["ready"] = [0]
            elif case == "pending_root":
                trace["boundaries"][0]["pending"] = [1]
            elif case == "illegal_action":
                trace["boundaries"][0]["legal"] = False
            elif case == "missing_public_event":
                executed["public_executions"] = []
            elif case == "private_event":
                executed["public_executions"][0]["data"]["private_to"] = 1
            elif case == "wrong_public_card":
                executed["public_executions"][0]["card_id"] += 1
            elif case == "rejection":
                executed["outcomes"][next(iter(executed["outcomes"]))] = "rejected"
            elif case == "missing_outcome":
                executed["outcomes"] = {}
            elif case == "invalid_delay":
                next(action for group in trace["boundaries"] for action in group["actions"].values()
                    if action["card_id"] is not None)["delay_slot"] = 0
            elif case == "wrong_ready":
                executed["ready"] = []
            elif case == "wrong_lifecycle":
                executed["evidence"][0]["reason"] = "preempted"
            elif case == "truncation":
                trace["ticks"][-1]["truncated"] = True
            elif case == "early_terminal":
                trace["ticks"][0]["ended"] = True
            elif case == "false_draw":
                trace["result"]["winner"] = None
            elif case == "wrong_label":
                trace["result"]["labels"] = ["WIN", "LOSS"]
            elif case == "wrong_draw_count":
                native["incomplete_draw_groups"] += 1
            elif case == "invalid_draw_size":
                trace["boundaries"][0]["draw_sizes"][0]["hand"] = -1
            elif case == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
            elif case == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "old_engine":
                report["engine_sha256"] = "0"*64
            elif case == "unclean_engine":
                shard["cleanup"]["engines"][0]["listening"] = True
                replace_artifact(prefix+"cleanup.json", shard["cleanup"])
            elif case == "training_claim":
                native["training_admissible"] = True
            native["trace_sha256"] = replace_artifact(prefix+"trace.json", trace)
            replace_artifact(prefix+"report.json", native)
            try:
                validate_boundary_coverage(report, root=temporary)
            except ValueError:
                rejected.append(case)
            else:
                raise AssertionError("accepted altered boundary evidence: "+case)
    closure_rejected = []
    for case in CLOSURE_CASES:
        report = deepcopy(closure)
        if case == "missing_assumption":
            report["assumptions"].pop()
        elif case == "missing_exclusion":
            report["exclusions"].pop()
        elif case == "wrong_cases":
            report["cases"] -= 1
        elif case == "wrong_histogram":
            report["duration_histogram"]["1"] += 1
        elif case == "wrong_source":
            report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
        elif case == "wrong_enumerator":
            report["enumerator_sha256"] = "0"*64
        elif case == "missing_counterexample":
            report["counterexamples"]["rejection_single_ready"] = False
        elif case == "training_claim":
            report["training_admissible"] = True
        try:
            validate_closure(report)
        except ValueError:
            closure_rejected.append(case)
        else:
            raise AssertionError("accepted altered closure evidence: "+case)
    result = dict(status="passed", training_admissible=False, rejected=rejected,
        closure_rejected=closure_rejected, count=len(rejected)+len(closure_rejected),
        validator_sha256=hashlib.sha256((ROOT/"verify_boundary_coverage.py").read_bytes()).hexdigest(),
        test_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        baseline_report_sha256=hashlib.sha256((ROOT/"native-boundary-coverage-report.json").read_bytes()).hexdigest(),
        closure_report_sha256=hashlib.sha256((ROOT/"boundary-closure-report.json").read_bytes()).hexdigest())
    (ROOT/"boundary-coverage-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
