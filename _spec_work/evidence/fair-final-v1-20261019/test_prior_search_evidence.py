from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_prior_search import ROOT, validate_prior_search
from native_runner.training.fair_search.contracts import semantic_digest


CASES = ("missing_stage", "short_horizon", "fake_budget", "hidden_input",
    "changed_root", "wrong_prior", "wrong_seed", "wrong_completion", "unchecked_native",
    "wrong_native_hand", "wrong_fifo", "wrong_slot", "multiple_worlds", "fewer_restores",
    "snapshot_leak", "missing_attempt", "broken_chain", "unfinished_backup",
    "changed_target", "changed_behavior", "changed_source", "changed_artifact",
    "unclean_engine", "training_claim", "hidden_interference", "unchanged_hidden")


def main():
    baseline = validate_prior_search()
    checks = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-prior-search-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_prior_search_validation.py", "prior_search_worker.py", "public-deck-prior-engineering.json"):
                (temporary/name).symlink_to(ROOT/name)

            def replace_artifact(suffix, payload):
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                target = temporary/relative
                target.unlink()
                target.write_text(json.dumps(payload))
                report["artifacts"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()

            shard = report["shards"][0]
            native = shard["report"]
            entry = native["records"][0]
            manifest = entry["audit"]["world_manifest"]
            conditioning = manifest["conditioning"]
            if case == "missing_stage":
                report["shards"].pop()
            elif case == "short_horizon":
                native["limits"]["soft_decisions"] = 2
            elif case == "fake_budget":
                entry["outcome"]["result"]["candidates"][0]["visits"] += 1
            elif case == "hidden_input":
                suffix = shard["job"]["name"]+"/history-0.json"
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                history = json.loads((ROOT/relative).read_text())
                history["actual_match_seed"] = 78101961
                replace_artifact(suffix, history)
            elif case == "changed_root":
                entry["fair_sha"] = "0"*64
            elif case == "wrong_prior":
                conditioning["prior_sha"] = "0"*64
            elif case == "wrong_seed":
                conditioning["seed_commitment"] = "0"*64
            elif case == "wrong_completion":
                conditioning["completion_manifest"]["completion_commitment"] = "0"*64
            elif case == "unchecked_native":
                conditioning["native_draw_verified"] = False
            elif case == "wrong_native_hand":
                draw = conditioning["native_current_draw"]
                draw[0], draw[4] = draw[4], draw[0]
            elif case == "wrong_fifo":
                draw = conditioning["native_current_draw"]
                draw[4], draw[5] = draw[5], draw[4]
            elif case == "wrong_slot":
                conditioning["logical_hand_order_differs"] = not conditioning["logical_hand_order_differs"]
            elif case == "multiple_worlds":
                entry["world_lifecycle"]["builds"] = 2
            elif case == "fewer_restores":
                entry["world_lifecycle"]["restores"] -= 1
            elif case == "snapshot_leak":
                entry["world_lifecycle"]["snapshot_released"] = False
            elif case == "missing_attempt":
                entry["attempts"].pop()
            elif case == "broken_chain":
                entry["attempts"][0]["transitions"][0]["after_tick"] += 1
            elif case == "unfinished_backup":
                entry["outcome"]["result"]["unfinished_edge_backups"] = 1
            elif case == "changed_target":
                entry["outcome"]["target"][0][1] += 1
            elif case == "changed_behavior":
                entry["outcome"]["selected"] = "invalid-action"
            elif case == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
            elif case == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "unclean_engine":
                shard["cleanup"]["engines"][0]["listening"] = True
                replace_artifact(shard["job"]["name"]+"/cleanup.json", shard["cleanup"])
            elif case == "training_claim":
                native["training_admissible"] = True
            elif case == "hidden_interference":
                native["records"][1]["world_lifecycle"]["root_digest"] = "0"*64
            elif case == "unchanged_hidden":
                mutation = native["records"][1]["mutation"]
                mutation["after_digest"] = mutation["before_digest"]
            entry["outcome"]["result"]["world_commitment"] = semantic_digest(manifest)
            replace_artifact(shard["job"]["name"]+"/report.json", native)
            try:
                validate_prior_search(report, root=temporary)
            except ValueError as error:
                checks.append(dict(case=case, rejected=True, error=str(error)))
            else:
                raise AssertionError("accepted tampered prior search evidence: "+case)
    result = dict(status="passed", roots=8, effective_simulations=1024, negative_cases=checks,
        report_sha256=hashlib.sha256((ROOT/"native-prior-search-report.json").read_bytes()).hexdigest())
    (ROOT/"prior-search-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(dict(status="passed", negative_cases=len(checks))))


if __name__ == "__main__":
    main()
