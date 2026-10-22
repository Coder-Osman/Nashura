from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_action_random import validate_action_random


CASES = ("missing_tick", "duplicate_tick", "target_present", "bad_count", "wrong_process",
    "state_write", "missing_stage", "restore_changed", "snapshot_leak", "incomplete_terminal",
    "wrong_labels", "missing_seat", "changed_source", "changed_artifact", "unclean_engine",
    "training_claim", "wrong_static_class")


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    original = validate_action_random(root, workspace)
    results = []
    for name in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-action-random-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(original)
            for relative in report["artifacts"]:
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(root / relative)
            for filename in ("run_action_random.py", "action_random_worker.py", "native-action-random-layout.json"):
                (temporary / filename).symlink_to(root / filename)

            def replace_artifact(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/" + suffix))
                target = temporary / relative
                if target.is_symlink():
                    target.unlink()
                target.write_text(json.dumps(payload))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            native = report["report"]
            relative = next(relative for relative in report["artifacts"] if relative.endswith("/samples.json"))
            trajectory = json.loads((root / relative).read_text())
            sample = trajectory["samples"][0]
            if name == "missing_tick":
                trajectory["samples"].pop(1)
                native["sampled_ticks"] -= 1
            elif name == "duplicate_tick":
                trajectory["samples"][1] = deepcopy(sample)
            elif name == "target_present":
                sample["matches"] = [dict(state=42)]
            elif name == "bad_count":
                sample["runtime_count"] += 1
                native["runtime_observations"] += 1
            elif name == "wrong_process":
                sample["child_pid"] += 1
            elif name == "state_write":
                sample["readonly"] = False
            elif name == "missing_stage":
                native["stages"].pop()
            elif name == "restore_changed":
                native["stages"][0]["restored"]["runtime_count"] += 1
            elif name == "snapshot_leak":
                native["stages"][0]["snapshot_released"] = False
            elif name == "incomplete_terminal":
                native["terminal"]["native_evidence"]["finalized"] = False
            elif name == "wrong_labels":
                native["terminal"]["labels"] = ["DRAW", "WIN"]
            elif name == "missing_seat":
                trajectory["decisions"] = [entry for entry in trajectory["decisions"] if entry["owner"] == 0]
                native["decisions"] = len(trajectory["decisions"])
            elif name == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
            elif name == "changed_artifact":
                relative = next(key for key in report["artifacts"] if key.endswith("/engine.log"))
                report["artifacts"][relative] = "0"*64
            elif name == "unclean_engine":
                report["cleanup"]["listening"] = True
                replace_artifact("cleanup.json", report["cleanup"])
            elif name == "training_claim":
                report["training_admissible"] = True
            elif name == "wrong_static_class":
                target = temporary / "native-action-random-layout.json"
                target.unlink()
                layout = json.loads((root / target.name).read_text())
                layout["reflected_class"] = "GenericBattleRandom"
                target.write_text(json.dumps(layout))
            replace_artifact("samples.json", trajectory)
            replace_artifact("report.json", native)
            (temporary / "native-action-random-report.json").write_text(json.dumps(report))
            try:
                validate_action_random(temporary, workspace)
            except ValueError as error:
                results.append(dict(case=name, rejected=True, error=str(error)))
            else:
                raise AssertionError("accepted tampered action random evidence: " + name)
    result = dict(status="passed", sampled_ticks=original["report"]["sampled_ticks"], negative_cases=results,
        report_sha256=hashlib.sha256((root / "native-action-random-report.json").read_bytes()).hexdigest())
    (root / "action-random-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(dict(status="passed", negative_cases=len(results))))


if __name__ == "__main__":
    main()
