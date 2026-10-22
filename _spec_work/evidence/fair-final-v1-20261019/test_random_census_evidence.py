from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_random_census import validate_random_census


CASES = ("missing_stage", "wrong_tick", "restore_state", "restore_digest", "changed_continuation",
    "missing_stale_check", "snapshot_leak", "wrong_process", "alternate_branch", "state_write",
    "changed_source", "changed_artifact", "unclean_engine", "training_claim", "wrong_inventory")


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    original = validate_random_census(root, workspace)
    results = []
    for name in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-random-census-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(original)
            for relative in report["artifacts"]:
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(root / relative)
            for filename in ("run_random_census.py", "random_census_worker.py", "native-random-inventory.json"):
                (temporary / filename).symlink_to(root / filename)

            def replace_artifact(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/" + suffix))
                target = temporary / relative
                if target.is_symlink():
                    target.unlink()
                target.write_text(json.dumps(payload))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            native = report["report"]
            entry = native["records"][0]
            if name == "missing_stage":
                native["records"].pop()
            elif name == "wrong_tick":
                entry["before"]["tick"] = 0
            elif name == "restore_state":
                entry["restored"]["manager_state"] += 1
            elif name == "restore_digest":
                entry["restored_digest"] = "0" * 64
            elif name == "changed_continuation":
                entry["continuations"][1]["census"]["manager_state"] += 1
            elif name == "missing_stale_check":
                entry["stale_rejected"].pop()
            elif name == "snapshot_leak":
                entry["snapshot_released"] = False
            elif name in ("wrong_process", "alternate_branch", "state_write"):
                for census in (entry["before"], entry["restored"],
                        *(item["census"] for item in entry["continuations"])):
                    if name == "wrong_process":
                        census["child_pid"] += 1
                    elif name == "alternate_branch":
                        census["alternate_50_present"] = True
                        census["alternate_50_states"] = {"0x550": 1, "0x554": 2, "0x558": 3}
                    else:
                        census["readonly"] = False
            elif name == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0" * 64
            elif name == "changed_artifact":
                relative = next(key for key in report["artifacts"] if key.endswith("/engine.log"))
                report["artifacts"][relative] = "0" * 64
            elif name == "unclean_engine":
                report["cleanup"]["listening"] = True
                replace_artifact("cleanup.json", report["cleanup"])
            elif name == "training_claim":
                report["training_admissible"] = True
            elif name == "wrong_inventory":
                target = temporary / "native-random-inventory.json"
                target.unlink()
                inventory = json.loads((root / target.name).read_text())
                inventory["calls"]["xorshift_generate"].pop()
                target.write_text(json.dumps(inventory))
            replace_artifact("report.json", native)
            replace_artifact("progress.json", dict(records=native["records"]))
            (temporary / "native-random-census-report.json").write_text(json.dumps(report))
            try:
                validate_random_census(temporary, workspace)
            except ValueError as error:
                results.append(dict(case=name, rejected=True, error=str(error)))
            else:
                raise AssertionError("accepted tampered random census: " + name)
    result = dict(status="passed", positive_stages=4, negative_cases=results,
        report_sha256=hashlib.sha256((root / "native-random-census-report.json").read_bytes()).hexdigest())
    (root / "random-census-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(dict(status="passed", positive_stages=4, negative_cases=len(results))))


if __name__ == "__main__":
    main()
