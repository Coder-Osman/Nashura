from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_single_ready_evidence import validate_single_ready_evidence


NEGATIVE_CASES = ("missing_seat", "training_claim", "normal_root_claim", "legal_injection_claim",
    "missing_native_rejection", "pending_deadline", "replay_divergence", "early_cancel",
    "same_tick_preemption", "opponent_rng_changed", "changed_source", "changed_artifact", "unclean_engine")


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    baseline = validate_single_ready_evidence(root, workspace)
    results = []
    for name in NEGATIVE_CASES:
        with tempfile.TemporaryDirectory(prefix="fair-single-ready-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to((root / relative).resolve())
            (temporary / "run_single_ready_validation.py").symlink_to(root / "run_single_ready_validation.py")

            def replace_artifact(filename, payload):
                relative = next(relative for relative in report["artifacts"] if Path(relative).name == filename)
                target = temporary / relative
                if target.is_symlink():
                    target.unlink()
                target.write_text(json.dumps(payload, sort_keys=True))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            if name == "missing_seat":
                report["records"].pop()
            elif name == "training_claim":
                report["training_admissible"] = True
            elif name == "normal_root_claim":
                report["normal_training_single_ready_verified"] = True
            elif name == "legal_injection_claim":
                report["records"][0]["payment"]["legal"] = True
            elif name == "missing_native_rejection":
                report["records"][0]["native_outcomes"] = []
            elif name == "pending_deadline":
                report["records"][0]["root"]["clock"]["pending"][0]["due_tick"] += 2
            elif name == "replay_divergence":
                report["records"][0]["branches"][0]["replays"][1]["final"]["native_digest"] = "0" * 64
            elif name == "early_cancel":
                for replay in report["records"][0]["branches"][0]["replays"]:
                    replay["trace"][0]["pending"] = []
            elif name == "same_tick_preemption":
                for replay in report["records"][0]["branches"][2]["replays"]:
                    for event in replay["final"]["clock"]["evidence"][-2:]:
                        if event["owner"] == 1:
                            event["reason"] = "preempted"
            elif name == "opponent_rng_changed":
                for replay in report["records"][0]["branches"][0]["replays"]:
                    replay["final"]["rng"]["1"] = "0" * 64
            elif name == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0" * 64
            elif name == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0" * 64
            elif name == "unclean_engine":
                report["cleanup"]["listening"] = True
                replace_artifact("cleanup.json", report["cleanup"])
            replace_artifact("report.json", {key: value for key, value in report.items()
                if key not in ("runner_sha256", "artifacts", "cleanup")})
            (temporary / "native-single-ready-report.json").write_text(json.dumps(report))
            try:
                validate_single_ready_evidence(temporary, workspace)
            except ValueError as error:
                results.append(dict(case=name, rejected=True, reason=str(error)))
            else:
                raise AssertionError(f"single-ready validator accepted {name}")
    result = dict(status="passed", positive_checks=6, negative_cases=results,
        report_sha256=hashlib.sha256((root / "native-single-ready-report.json").read_bytes()).hexdigest())
    (root / "single-ready-evidence-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
