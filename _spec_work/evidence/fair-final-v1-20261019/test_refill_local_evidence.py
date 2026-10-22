from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from verify_refill_local import ROOT, validate_refill_local


CASES = ("missing_root", "wrong_seed", "changed_anchor_completion", "changed_completion",
    "changed_mapping", "changed_root", "changed_owner", "replayed_history", "changed_holes",
    "changed_own_draw", "changed_native_slots", "missing_baseline", "changed_refill_time",
    "changed_fifo", "changed_continuation", "changed_anchor", "unrestored_anchor",
    "changed_rollback", "changed_incremental_ticks", "changed_construction", "changed_commitment", "stale_root_tick",
    "training_claim", "changed_scope", "changed_prior", "changed_model", "changed_source",
    "changed_payload", "changed_artifact", "changed_engine", "changed_launcher", "unclean_engine",
    "changed_history", "changed_progress", "bad_duration")


def main():
    baseline = validate_refill_local()
    rejected = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="fair-refill-local-negative-") as directory:
            temporary = Path(directory)
            report = deepcopy(baseline)
            for relative in report["artifacts"]:
                destination = temporary/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(ROOT/relative)
            for name in ("run_refill_local_validation.py", "refill_local_worker.py",
                    "public-deck-prior-engineering.json", "phase_snapshot_launcher.py"):
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
            if case == "missing_root": native["records"].pop()
            elif case == "wrong_seed": row["seed"] += 1
            elif case == "changed_anchor_completion": row["anchor_completion"]["initial_draw"].reverse()
            elif case == "changed_completion": row["completion"]["current_draw"].reverse()
            elif case == "changed_mapping": row["completion"]["manifest"]["public_cards_fixed"] = []
            elif case == "changed_root": row["manifest"]["root_fair_sha"] = "0"*64
            elif case == "changed_owner": row["manifest"]["owner"] = 1
            elif case == "replayed_history": row["manifest"]["history_ticks_replayed"] = 1
            elif case == "changed_holes": row["manifest"]["refill_holes"] = []
            elif case == "changed_own_draw": row["local_layouts"][0]["hand"].reverse()
            elif case == "changed_native_slots": row["local_layouts"][1]["cycle_slots"].reverse()
            elif case == "missing_baseline": row.pop("baseline")
            elif case == "changed_refill_time": row["baseline"]["ticks"][0] = deepcopy(row["baseline"]["ticks"][-1])
            elif case == "changed_fifo":
                row["continuations"][0]["ticks"][0]["layouts"][1]["cycle"].reverse()
                row["continuations"][1] = deepcopy(row["continuations"][0])
            elif case == "changed_continuation": row["continuations"][1]["digest"] = "0"*64
            elif case == "changed_anchor": native["records"][1]["anchor_digest"] = "0"*64
            elif case == "unrestored_anchor": row["anchor_restored"] = False
            elif case == "changed_rollback": native["rollbacks"][0]["restored_digest"] = "0"*64
            elif case == "changed_incremental_ticks": native["syncs"][1]["profile"]["last_sync"]["ticks"] += 1
            elif case == "changed_construction": native["syncs"][1]["construction_manifest"]["actual_state_copied"] = True
            elif case == "changed_commitment": native["syncs"][1]["construction_manifest"]["completion_manifest"]["completion_commitment"] = "0"*64
            elif case == "stale_root_tick": native["syncs"][1]["construction_manifest"]["root_tick"] = row["tick"]
            elif case == "training_claim": native["training_admissible"] = True
            elif case == "changed_scope": native["exclusions"] = []
            elif case == "changed_prior": native["prior_sha"] = "0"*64
            elif case == "changed_model": native["checkpoint_sha256"] = "0"*64
            elif case == "changed_source": report["source_sha256"]["native_runner/training_capture.py"] = "0"*64
            elif case == "changed_payload":
                report["payload_sha256"]["project/run.py"] = "0"*64
                attempt = read("attempt.json")
                attempt["payload_sha256"] = deepcopy(report["payload_sha256"])
                replace("attempt.json", attempt)
            elif case == "changed_artifact": report["artifacts"][next(iter(report["artifacts"]))] = "0"*64
            elif case == "changed_engine": cleanup["process"]["phase_snapshot_engine"]["sha256"] = "0"*64
            elif case == "changed_launcher": cleanup["process"]["phase_snapshot_engine"]["launcher_sha256"] = "0"*64
            elif case == "unclean_engine": cleanup["listening"] = True
            elif case == "changed_history":
                history = read("project/"+shard["job"]["next_history"])
                history["root_tick"] += 1
                replace("project/"+shard["job"]["next_history"], history)
            elif case == "bad_duration": row["seconds"] = -1
            if case != "changed_artifact":
                prefix = shard["job"]["name"]+"/"
                replace(prefix+"report.json", native)
                progress = dict(records=native["records"], syncs=native["syncs"])
                if case == "changed_progress": progress["records"] = []
                replace(prefix+"progress.json", progress)
                replace(prefix+"cleanup.json", cleanup)
                replace(prefix+f"engine-{shard['job']['port']}/process.json", cleanup["process"])
                results = read("run-results.json")
                results[0] = cleanup
                replace("run-results.json", results)
            try:
                validate_refill_local(report, root=temporary)
            except ValueError:
                rejected.append(case)
            else:
                raise AssertionError("tampered refill local evidence accepted: "+case)
    result = dict(schema="fair-final-refill-local-rejections.v1", status="passed", training_admissible=False,
        count=len(rejected), rejected=rejected, **{key: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
            for key, name in (("baseline_report_sha256", "native-refill-local-report.json"),
                ("validator_sha256", "verify_refill_local.py"), ("test_sha256", Path(__file__).name))})
    (ROOT/"refill-local-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
