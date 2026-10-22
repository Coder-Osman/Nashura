from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_refill_provider import ROOT, validate_refill_provider


CASES = ("missing_root", "fake_budget", "changed_target", "changed_behavior", "root_noise",
    "changed_candidate_mass", "changed_boundary", "changed_attempts", "unfinished_backups",
    "changed_phase_restore", "unreleased_snapshot", "changed_hidden_pair", "no_hidden_change",
    "changed_anchor_completion", "changed_completion", "changed_holes", "changed_own_draw",
    "changed_slots", "changed_fifo", "replayed_history", "changed_incremental_ticks",
    "changed_anchor_tick", "changed_cursor", "changed_history", "changed_replay", "changed_progress",
    "changed_world_commitment", "changed_model", "changed_prior", "changed_source", "changed_payload",
    "changed_artifact", "changed_engine", "changed_launcher", "shared_engine", "unclean_engine",
    "training_claim", "changed_scope", "bad_duration", "no_depth_coverage")


def main():
    baseline = validate_refill_provider()
    rejected, reasons = [], {}
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-refill-provider-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_refill_provider_validation.py", "refill_provider_worker.py"):
                (temporary/name).symlink_to(ROOT/name)

            def read(suffix):
                relative = next(name for name in report["artifacts"] if name.endswith("/"+suffix))
                return json.loads((temporary/relative).read_text())

            def replace(suffix, value):
                relative = next(name for name in report["artifacts"] if name.endswith("/"+suffix))
                destination = temporary/relative
                destination.unlink()
                destination.write_text(json.dumps(value))
                report["artifacts"][relative] = hashlib.sha256(destination.read_bytes()).hexdigest()

            shard = report["shards"][0]
            native, cleanup = shard["report"], shard["cleanup"]
            row = native["records"][0]
            result = row["outcome"]["result"]
            manifest = row["audit"]["world_manifest"]
            if case == "missing_root": native["records"].pop()
            elif case == "fake_budget": result["valid_simulations"] = 127
            elif case == "changed_target": row["outcome"]["target"][0][1] += 1
            elif case == "changed_behavior": row["outcome"]["selected"] = "ACT:invalid"
            elif case == "root_noise": result["noise_fraction"] = .25
            elif case == "changed_candidate_mass": result["candidates"][0]["prior_projected"] = .9
            elif case == "changed_boundary": row["attempts"][0]["transitions"][0]["before_tick"] += 1
            elif case == "changed_attempts": row["attempts"].pop()
            elif case == "unfinished_backups": result["unfinished_edge_backups"] = 1
            elif case == "changed_phase_restore": row["world_lifecycle"]["phase_restores"] -= 1
            elif case == "unreleased_snapshot": row["world_lifecycle"]["snapshot_released"] = False
            elif case == "changed_hidden_pair": native["records"][1]["world_lifecycle"]["root_digest"] = "0"*64
            elif case == "no_hidden_change":
                mutation = native["records"][1]["mutation"]
                mutation["before_digest"] = mutation["after_digest"]
            elif case == "changed_anchor_completion": row["anchor_completion"]["initial_draw"].reverse()
            elif case == "changed_completion": manifest["conditioning"]["completion_manifest"]["completion_commitment"] = "0"*64
            elif case == "changed_holes": manifest["conditioning"]["refill_holes"] = []
            elif case == "changed_own_draw": row["local_layouts"][0]["hand"].reverse()
            elif case == "changed_slots": row["local_layouts"][1]["cycle_slots"].reverse()
            elif case == "changed_fifo": row["local_layouts"][1]["cycle"].reverse()
            elif case == "replayed_history": manifest["conditioning"]["history_ticks_replayed"] = 1
            elif case == "changed_incremental_ticks": native["records"][2]["anchor_profile"]["incremental_ticks"] += 1
            elif case == "changed_anchor_tick": native["records"][2]["audit"]["world_manifest"]["anchor"]["root_tick"] = row["tick"]
            elif case == "changed_cursor": native["cursor_events"][2]["from_tick"] = 0
            elif case == "changed_history":
                history = read(shard["job"]["name"]+"/history-successor-0.json")
                history["root_tick"] += 1
                replace(shard["job"]["name"]+"/history-successor-0.json", history)
            elif case == "changed_replay": native["replayed"].pop()
            elif case == "changed_world_commitment": result["world_commitment"] = "0"*64
            elif case == "changed_model": native["checkpoint_sha256"] = "0"*64
            elif case == "changed_prior": native["prior_sha"] = "0"*64
            elif case == "changed_source": report["source_sha256"]["native_runner/training_capture.py"] = "0"*64
            elif case == "changed_payload":
                report["payload_sha256"]["project/run.py"] = "0"*64
                attempt = read("attempt.json")
                attempt["payload_sha256"] = deepcopy(report["payload_sha256"])
                replace("attempt.json", attempt)
            elif case == "changed_artifact": report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "changed_engine": cleanup["engines"][0]["process"]["phase_snapshot_engine"]["sha256"] = "0"*64
            elif case == "changed_launcher": cleanup["engines"][0]["process"]["phase_snapshot_engine"]["launcher_sha256"] = "0"*64
            elif case == "shared_engine": cleanup["engines"][1]["process"]["child_pid"] = cleanup["engines"][0]["process"]["child_pid"]
            elif case == "unclean_engine": cleanup["engines"][0]["listening"] = True
            elif case == "training_claim": native["training_admissible"] = True
            elif case == "changed_scope": native["exclusions"] = []
            elif case == "bad_duration": row["local_profile"]["seconds"] = -1
            elif case == "no_depth_coverage":
                for item in report["shards"]:
                    for entry in item["report"]["records"]:
                        lifecycle = Counter()
                        for attempt in entry["attempts"]:
                            transition = attempt["transitions"][0]
                            attempt["transitions"] = [transition]
                            if transition["completed"]:
                                transition["decisions"] = 1
                                transition["ticks"] = 1
                                if transition["terminal"] is None:
                                    transition["after_tick"] = transition["before_tick"]+1
                                lifecycle[transition["reason"]] += 1
                            attempt["decisions"] = int(transition["completed"])
                            attempt["ticks"] = int(transition["completed"])
                        entry["audit"]["completed_lifecycle"] = dict(lifecycle)
                        entry["audit"]["discarded_edges"] = sum(not attempt["transitions"][0]["completed"]
                            for attempt in entry["attempts"])
            if case != "changed_artifact":
                for item in report["shards"]:
                    prefix = item["job"]["name"]+"/"
                    replace(prefix+"report.json", item["report"])
                    progress = dict(records=item["report"]["records"], replayed=item["report"]["replayed"])
                    if case == "changed_progress" and item is shard:
                        progress["records"] = []
                    replace(prefix+"progress.json", progress)
                    replace(prefix+"cleanup.json", item["cleanup"])
                    for engine in item["cleanup"]["engines"]:
                        replace(prefix+f"engine-{engine['port']}/process.json", engine["process"])
                replace("run-results.json", [item["cleanup"] for item in report["shards"]])
            try:
                validate_refill_provider(report, root=temporary)
            except ValueError as error:
                if case == "no_depth_coverage" and str(error) != "refill provider engine independence or suite depth coverage missing":
                    raise AssertionError("depth-coverage negative failed before reaching suite depth validation") from error
                rejected.append(case)
                reasons[case] = str(error)
            else:
                raise AssertionError("tampered refill provider evidence accepted: "+case)
    result = dict(schema="fair-final-refill-provider-rejections.v1", status="passed", training_admissible=False,
        count=len(rejected), rejected=rejected, rejection_reasons=reasons, searches=24, effective_simulations=3072,
        **{key: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for key, name in (
            ("baseline_report_sha256", "native-refill-provider-report.json"),
            ("validator_sha256", "verify_refill_provider.py"),
            ("attempt_validator_sha256", "verify_prior_search.py"), ("test_sha256", Path(__file__).name))})
    (ROOT/"refill-provider-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
