import hashlib
import json
from pathlib import Path
import sys


def validate_random_census(root, workspace):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from tools.fair_final_prng_audit import LIBG_SHA, PROBE_SHA
    from tools.fair_final_random_census import semantic_census
    from tools.fair_final_random_inventory import build_inventory
    from tools.probe_fair_final_random_census import SOURCE_SHA, THRESHOLDS
    from native_runner.training.fair_search.contracts import require_sha

    inventory = json.loads((root / "native-random-inventory.json").read_text())
    actual = build_inventory(workspace / "firstlight_CR/artifacts/standalone-engine-20260926/app-libs/libg.so")
    actual["generator_sha256"] = hashlib.sha256(
        (workspace / "firstlight_CR/tools/fair_final_random_inventory.py").read_bytes()).hexdigest()
    if json.loads(json.dumps(actual)) != inventory or inventory["libg_sha256"] != LIBG_SHA:
        raise ValueError("random inventory does not reproduce from fixed binary")
    report = json.loads((root / "native-random-census-report.json").read_text())
    if (report["schema"] != "fair-final-random-census-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["port"] != 29648
            or not report["source_sha256"] or not report["artifacts"]):
        raise ValueError("random census suite identity differs")
    for name, key in (("run_random_census.py", "runner_sha256"), ("random_census_worker.py", "worker_sha256")):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("random census runner or worker differs")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("random census source changed")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("random census artifact changed")

    def artifact(suffix):
        paths = [root / relative for relative in report["artifacts"] if relative.endswith("/" + suffix)]
        if len(paths) != 1:
            raise ValueError("random census artifact missing or ambiguous: " + suffix)
        return json.loads(paths[0].read_text())

    if artifact("project/probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("random census source deployment differs")
    workers = [value for key, value in report["artifacts"].items() if key.endswith("/project/run.py")]
    if workers != [report["worker_sha256"]]:
        raise ValueError("random census deployed worker differs")
    attempt, registration, preflight = artifact("attempt.json"), artifact("run-manifest.json"), artifact("preflight.json")
    if (attempt["exit_code"] != 0 or any(attempt[key] != report[key] for key in
            ("remote", "port", "source_sha256", "runner_sha256", "worker_sha256"))
            or registration["root"] != report["remote"] or registration["port"] != report["port"]
            or preflight["engine_sha256"] != PROBE_SHA or preflight["memory_available"] < 32 * 1024**3
            or preflight["free_bytes"] < 5 * 1024**3):
        raise ValueError("random census registration or resource identity differs")
    native, cleanup = report["report"], report["cleanup"]
    if (artifact("report.json") != native or artifact("cleanup.json") != cleanup
            or artifact("engine/process.json") != cleanup["process"]
            or native["schema"] != "fair-final-native-random-census.v1" or native["status"] != "passed"
            or native["training_admissible"] is not False or native["source_sha256"] != report["source_sha256"]
            or native["checkpoint_sha256"] != SOURCE_SHA or native["model_unchanged"] is not True
            or not native["exclusions"]):
        raise ValueError("random census native report differs")
    if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
            or cleanup["listening"] is not False or any(cleanup["pids_alive"].values())
            or cleanup["process"]["phase"] != "stopped" or cleanup["port"] != report["port"]):
        raise ValueError("random census owned process not cleaned")
    records = native["records"]
    if [entry["threshold"] for entry in records] != list(THRESHOLDS):
        raise ValueError("random census stage coverage differs")
    if artifact("progress.json") != dict(records=records):
        raise ValueError("random census progress differs")
    for entry in records:
        before, restored = entry["before"], entry["restored"]
        continuations = entry["continuations"]
        if (not entry["threshold"] <= before["tick"] <= entry["threshold"] + 60
                or semantic_census(before) != semantic_census(restored)
                or entry["before_digest"] != entry["after_read_digest"]
                or entry["before_digest"] != entry["restored_digest"]
                or entry["raw_unchanged"] is not True or entry["snapshot_released"] is not True
                or entry["stale_rejected"] != ["generation", "stateEpoch", "tick"]
                or len(continuations) != 2):
            raise ValueError("random census boundary or restore evidence differs")
        require_sha(entry["before_digest"])
        comparable = [{**item, "census": semantic_census(item["census"])} for item in continuations]
        if comparable[0] != comparable[1]:
            raise ValueError("random census repeated continuation differs")
        if (not continuations[0]["actions"] or continuations[0]["census"]["tick"] <= before["tick"]
                or continuations[0]["digest"] == entry["before_digest"]):
            raise ValueError("random census continuation did not advance")
        for census in (before, restored, *(item["census"] for item in continuations)):
            if (any(census[key] is not True for key in ("readonly", "world_present", "world_backlink_matches"))
                    or census["child_pid"] != cleanup["process"]["child_pid"]
                    or int(census["child_start_ticks"]) != int(cleanup["process"]["child_start_ticks"])
                    or type(census["manager_state"]) is not int or not 0 <= census["manager_state"] < 2**32
                    or census["alternate_50_present"] is not False or census["alternate_50_states"] is not None
                    or census["alternate_218_present"] is not False):
                raise ValueError("random census pointer, process, or branch evidence differs")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    report = validate_random_census(root, root.parents[2])
    print(json.dumps(dict(status="passed", stages=len(report["report"]["records"]),
        scope="read-only object presence and deterministic snapshot continuation only")))
