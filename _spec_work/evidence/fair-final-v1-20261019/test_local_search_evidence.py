from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_local_search import ROOT, validate_local_search
from native_runner.training.fair_search.contracts import semantic_digest


CASES = ("missing_shard", "missing_successor", "short_horizon", "fake_budget",
    "hidden_input", "wrong_seed", "wrong_completion", "wrong_fifo", "copied_actual",
    "history_replay_claim", "anchor_leak", "anchor_not_retained", "missing_phase_restore",
    "missing_attempt", "unfinished_backup", "changed_target", "changed_behavior",
    "hidden_interference", "unchanged_hidden", "successor_cold_rebuild",
    "anchor_delta", "wrong_anchor_seed", "changed_source", "changed_artifact",
    "old_engine", "unclean_engine", "training_claim")


def main():
    baseline = validate_local_search()
    rejected = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-local-search-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_local_search_validation.py", "local_search_worker.py",
                    "public-deck-prior-engineering.json", "phase_snapshot_launcher.py"):
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
            if case == "missing_shard":
                report["shards"].pop()
            elif case == "missing_successor":
                native["records"].pop()
            elif case == "short_horizon":
                native["limits"]["soft_decisions"] = 2
            elif case == "fake_budget":
                entry["outcome"]["result"]["candidates"][0]["visits"] += 1
            elif case == "hidden_input":
                suffix = shard["job"]["name"]+"/history-initial-0.json"
                relative = next(relative for relative in report["artifacts"] if relative.endswith("/"+suffix))
                history = json.loads((ROOT/relative).read_text())
                history["actual_match_seed"] = 78101961
                replace_artifact(suffix, history)
            elif case == "wrong_seed":
                conditioning["seed_commitment"] = "0"*64
            elif case == "wrong_completion":
                conditioning["completion_manifest"]["completion_commitment"] = "0"*64
            elif case == "wrong_fifo":
                draw = conditioning["native_current_draw"]
                draw[4], draw[5] = draw[5], draw[4]
            elif case == "copied_actual":
                manifest["actual_state_copied"] = True
            elif case == "history_replay_claim":
                conditioning["history_ticks_replayed"] = 1
            elif case == "anchor_leak":
                native["anchor_snapshot_released"] = False
            elif case == "anchor_not_retained":
                entry["anchor_retained"] = False
            elif case == "missing_phase_restore":
                entry["world_lifecycle"]["phase_restores"] -= 1
            elif case == "missing_attempt":
                entry["attempts"].pop()
            elif case == "unfinished_backup":
                entry["outcome"]["result"]["unfinished_edge_backups"] = 1
            elif case == "changed_target":
                entry["outcome"]["target"][0][1] += 1
            elif case == "changed_behavior":
                entry["outcome"]["selected"] = "invalid-action"
            elif case == "hidden_interference":
                native["records"][1]["world_lifecycle"]["root_digest"] = "0"*64
            elif case == "unchanged_hidden":
                mutation = native["records"][1]["mutation"]
                mutation["after_digest"] = mutation["before_digest"]
            elif case == "successor_cold_rebuild":
                native["records"][2]["local_profile"]["anchor_event"]["mode"] = "rebuild"
            elif case == "anchor_delta":
                native["records"][2]["anchor_profile"]["incremental_ticks"] += 1
            elif case == "wrong_anchor_seed":
                manifest["anchor"]["seed_commitment"] = "0"*64
            elif case == "changed_source":
                report["source_sha256"][next(iter(report["source_sha256"]))] = "0"*64
            elif case == "changed_artifact":
                report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "old_engine":
                report["engine_sha256"] = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"
            elif case == "unclean_engine":
                shard["cleanup"]["engines"][0]["listening"] = True
                replace_artifact(shard["job"]["name"]+"/cleanup.json", shard["cleanup"])
            elif case == "training_claim":
                native["training_admissible"] = True
            entry["outcome"]["result"]["world_commitment"] = semantic_digest(manifest)
            replace_artifact(shard["job"]["name"]+"/report.json", native)
            try:
                validate_local_search(report, root=temporary)
            except ValueError:
                rejected.append(case)
            else:
                raise AssertionError("accepted altered local search evidence: "+case)
    result = dict(status="passed", training_admissible=False, roots=16, effective_simulations=2048,
        rejected=rejected, count=len(rejected), validator_sha256=hashlib.sha256((ROOT/"verify_local_search.py").read_bytes()).hexdigest(),
        baseline_report_sha256=hashlib.sha256((ROOT/"native-local-search-report.json").read_bytes()).hexdigest())
    (ROOT/"local-search-evidence-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
