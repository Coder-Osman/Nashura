from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_prior_history import ROOT, validate_prior_history


CASES = ("missing_root", "duplicate_root", "changed_completion", "unchecked_native_draw",
    "wrong_prior", "actual_state_copy", "unfinished_edge", "restore_changed", "snapshot_leak",
    "unchanged_hidden", "hidden_interference", "missing_successor", "wrong_delta",
    "changed_source", "changed_artifact", "unclean_engine", "training_claim", "wrong_seed",
    "wrong_native_hand", "wrong_native_cycle", "wrong_slot_annotation")


def main():
    original = validate_prior_history()
    checks = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-prior-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(original)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_prior_history_validation.py", "prior_history_worker.py", "public-deck-prior-engineering.json"):
                (temporary/name).symlink_to(ROOT/name)

            def replace_artifact(name, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+name))
                target = temporary/relative
                target.unlink()
                target.write_text(json.dumps(payload))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            native = report["report"]
            first = native["records"][0]
            if case == "missing_root":
                native["records"].pop()
            elif case == "duplicate_root":
                native["records"][1] = deepcopy(first)
            elif case == "changed_completion":
                first["initial_draw"].reverse()
            elif case == "unchecked_native_draw":
                first["conditioning"]["native_draw_verified"] = False
            elif case == "wrong_prior":
                first["conditioning"]["prior_sha"] = "0"*64
            elif case == "actual_state_copy":
                first["conditioning"]["actual_state_copied"] = True
            elif case == "unfinished_edge":
                first["continuations"][0]["completed"] = False
                first["continuations"][1]["completed"] = False
            elif case == "restore_changed":
                first["continuations"][1]["native_digest"] = "0"*64
            elif case == "snapshot_leak":
                first["snapshot_released"] = False
            elif case == "unchanged_hidden":
                row = next(row for row in native["records"] if row["variant"] == "hidden-draw-changed")
                row["mutation"]["after_digest"] = row["mutation"]["before_digest"]
            elif case == "hidden_interference":
                row = next(row for row in native["records"] if row["variant"] == "hidden-draw-changed")
                row["root_native_digest"] = "0"*64
            elif case == "missing_successor":
                native["incremental"].pop()
            elif case == "wrong_delta":
                native["incremental"][0]["delta"]["added_plays"] += 1
            elif case == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
            elif case == "changed_artifact":
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/probe.log"))
                report["artifacts"][relative] = "0"*64
            elif case == "unclean_engine":
                report["cleanup"]["engines"][0]["listening"] = True
                replace_artifact("cleanup.json", report["cleanup"])
            elif case == "training_claim":
                native["training_admissible"] = True
            elif case == "wrong_seed":
                first["conditioning"]["seed_commitment"] = "0"*64
            elif case == "wrong_native_hand":
                draw = first["conditioning"]["native_current_draw"]
                draw[0], draw[4] = draw[4], draw[0]
            elif case == "wrong_native_cycle":
                draw = first["conditioning"]["native_current_draw"]
                draw[4], draw[5] = draw[5], draw[4]
            elif case == "wrong_slot_annotation":
                first["conditioning"]["logical_hand_order_differs"] = not first["conditioning"]["logical_hand_order_differs"]
            replace_artifact("report.json", native)
            try:
                validate_prior_history(report, root=temporary)
            except ValueError as error:
                checks.append(dict(case=case, rejected=True, error=str(error)))
            else:
                raise AssertionError("accepted tampered native prior evidence: "+case)
    result = dict(status="passed", roots=8, successors=4, negative_cases=checks,
        report_sha256=hashlib.sha256((ROOT/"native-prior-history-report.json").read_bytes()).hexdigest())
    (ROOT/"prior-history-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(dict(status="passed", negative_cases=len(checks))))


if __name__ == "__main__":
    main()
