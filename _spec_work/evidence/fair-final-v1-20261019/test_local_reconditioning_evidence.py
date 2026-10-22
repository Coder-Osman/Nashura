from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_local_reconditioning import ROOT, validate_local_reconditioning


CASES = ("missing_root", "wrong_seed", "changed_root", "changed_draw", "replayed_history",
    "changed_continuation", "unrestored_anchor", "unrestored_phase", "empty_phase", "training_claim",
    "changed_source", "changed_artifact", "changed_engine", "changed_launcher", "unclean_engine",
    "changed_build", "false_actual_state", "missing_completion", "bad_timing")


def main():
    baseline = validate_local_reconditioning()
    rejected = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-local-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_local_reconditioning_validation.py", "local_reconditioning_worker.py",
                    "public-deck-prior-engineering.json"):
                (temporary/name).symlink_to(ROOT/name)

            def artifact(suffix):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                return json.loads((temporary/relative).read_text())

            def replace_artifact(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                target = temporary/relative
                target.unlink()
                target.write_text(json.dumps(payload))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            native, cleanup = report["report"], report["cleanup"]
            row = native["records"][0]
            if case == "missing_root": native["records"].pop()
            elif case == "wrong_seed": row["seed"] += 1
            elif case == "changed_root": row["manifest"]["root_fair_sha"] = "0"*64
            elif case == "changed_draw": row["manifest"]["native_current_draw"].reverse()
            elif case == "replayed_history": row["manifest"]["history_ticks_replayed"] = 1
            elif case == "changed_continuation": row["continuations"][1]["native_digest"] = "0"*64
            elif case == "unrestored_anchor": row["anchor_restored"] = False
            elif case == "unrestored_phase": row["native_phase_restored"] = False
            elif case == "empty_phase": row["native_phase_object_count"] = 0
            elif case == "training_claim": native["training_admissible"] = True
            elif case == "changed_source": report["source_sha256"]["native_runner/training_capture.py"] = "0"*64
            elif case == "changed_artifact": report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "changed_engine": cleanup["engines"][0]["process"]["phase_snapshot_engine"]["sha256"] = "0"*64
            elif case == "changed_launcher": cleanup["engines"][0]["process"]["phase_snapshot_engine"]["launcher_sha256"] = "0"*64
            elif case == "unclean_engine": cleanup["engines"][0]["listening"] = True
            elif case == "changed_build":
                build = artifact("engine-build-manifest.json")
                build["artifacts"]["libcrprobe.so"] = "0"*64
                replace_artifact("engine-build-manifest.json", build)
            elif case == "false_actual_state": row["manifest"]["actual_state_copied"] = True
            elif case == "missing_completion": row["manifest"]["native_draw_verified"] = False
            elif case == "bad_timing": row["seconds"] = -1
            if case != "changed_artifact":
                replace_artifact("report.json", native)
                replace_artifact("progress.json", dict(records=native["records"]))
                replace_artifact("cleanup.json", cleanup)
            try:
                validate_local_reconditioning(report, root=temporary)
            except ValueError:
                rejected.append(case)
            else:
                raise AssertionError("tampered local evidence accepted: "+case)
    result = dict(schema="fair-final-local-evidence-rejections.v1", status="passed",
        baseline_report_sha256=hashlib.sha256((ROOT/"native-local-reconditioning-report.json").read_bytes()).hexdigest(),
        validator_sha256=hashlib.sha256((ROOT/"verify_local_reconditioning.py").read_bytes()).hexdigest(),
        rejected=rejected, count=len(rejected), training_admissible=False)
    (ROOT/"local-reconditioning-evidence-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
