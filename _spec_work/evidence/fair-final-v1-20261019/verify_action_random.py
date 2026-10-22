import hashlib
import json
from pathlib import Path
import sys


def validate_action_random(root, workspace):
    sys.path.insert(0, str(workspace / "firstlight_CR"))
    from tools.fair_final_action_random import TARGET_VTABLE, static_layout
    from tools.fair_final_prng_audit import PROBE_SHA
    from tools.fair_final_random_census import semantic_census
    from tools.probe_fair_final_random_census import SOURCE_SHA, THRESHOLDS
    from native_runner.training.fair_search.contracts import require_sha
    from native_runner.battle_env import FIRST_PLAYABLE_TICK

    layout = json.loads((root / "native-action-random-layout.json").read_text())
    actual = static_layout(workspace / "firstlight_CR/artifacts/standalone-engine-20260926/app-libs/libg.so")
    actual["source_sha256"] = hashlib.sha256((workspace / "firstlight_CR/tools/fair_final_action_random.py").read_bytes()).hexdigest()
    if actual != layout:
        raise ValueError("action random static evidence differs")
    report = json.loads((root / "native-action-random-report.json").read_text())
    if (report["schema"] != "fair-final-action-random-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["port"] != 29649
            or not report["source_sha256"] or not report["artifacts"]):
        raise ValueError("action random suite identity differs")
    for name, key in (("run_action_random.py", "runner_sha256"), ("action_random_worker.py", "worker_sha256")):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("action random runner identity differs")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("action random source identity differs")
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("action random artifact identity differs")

    def artifact(suffix):
        paths = [root / relative for relative in report["artifacts"] if relative.endswith("/" + suffix)]
        if len(paths) != 1:
            raise ValueError("action random artifact missing or ambiguous")
        return json.loads(paths[0].read_text())

    if artifact("project/probe-source-manifest.json") != report["source_sha256"]:
        raise ValueError("action random deployed identity differs")
    if [value for key, value in report["artifacts"].items() if key.endswith("/project/run.py")] != [report["worker_sha256"]]:
        raise ValueError("action random deployed worker differs")
    attempt, registration, preflight = artifact("attempt.json"), artifact("run-manifest.json"), artifact("preflight.json")
    if (attempt["exit_code"] != 0 or any(attempt[key] != report[key] for key in
            ("remote", "port", "source_sha256", "runner_sha256", "worker_sha256"))
            or registration["root"] != report["remote"] or registration["port"] != report["port"]
            or preflight["engine_sha256"] != PROBE_SHA or preflight["memory_available"] < 32*1024**3
            or preflight["free_bytes"] < 5*1024**3):
        raise ValueError("action random registration or resources differ")
    native, cleanup = report["report"], report["cleanup"]
    if (artifact("report.json") != native or artifact("cleanup.json") != cleanup
            or artifact("engine/process.json") != cleanup["process"]
            or native["schema"] != "fair-final-native-action-random.v1" or native["status"] != "passed"
            or native["training_admissible"] is not False or native["source_sha256"] != report["source_sha256"]
            or native["checkpoint_sha256"] != SOURCE_SHA or native["model_unchanged"] is not True
            or not native["exclusions"]):
        raise ValueError("action random native report differs")
    if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
            or cleanup["listening"] is not False or any(cleanup["pids_alive"].values())
            or cleanup["process"]["phase"] != "stopped" or cleanup["port"] != report["port"]):
        raise ValueError("action random process cleanup incomplete")
    trajectory = artifact("samples.json")
    samples, decisions = trajectory["samples"], trajectory["decisions"]
    expected_ticks = sorted({entry["tick"] for entry in decisions} | set(THRESHOLDS) | {native["final_tick"]})
    if (not samples or native["start_tick"] != FIRST_PLAYABLE_TICK or native["final_tick"] <= 5400
            or native["sampling"] != "every-decision-plus-stage-and-terminal.v1"
            or [sample["tick"] for sample in samples] != expected_ticks
            or native["sampled_ticks"] != len(samples) or native["decisions"] != len(decisions)
            or native["matched_instances"] != 0
            or native["runtime_observations"] != sum(sample["runtime_count"] for sample in samples)
            or native["runtime_observations"] <= 0):
        raise ValueError("action random trajectory coverage differs")
    for sample in samples:
        if (sample["readonly"] is not True or sample["matches"] != []
                or sample["child_pid"] != cleanup["process"]["child_pid"]
                or int(sample["child_start_ticks"]) != int(cleanup["process"]["child_start_ticks"])
                or not 0 <= sample["queue_count"] <= sample["object_count"] <= 100000
                or sample["runtime_count"] != sum(sample["vtables"].values())
                or any(type(count) is not int or count < 1 for count in sample["vtables"].values())
                or hex(TARGET_VTABLE) in sample["vtables"]):
            raise ValueError("action random sample or absence evidence differs")
    terminal = native["terminal"]
    evidence = terminal["native_evidence"]
    winner = terminal["winner"]
    expected_result = -1 if winner is None else winner
    if (evidence["finalized"] is not True or winner not in (None, 0, 1)
            or evidence["worldResult"] != expected_result or evidence["worldResultRaw"] != expected_result
            or evidence["winner"] != winner or terminal["terminal_tick"] != native["final_tick"]
            or terminal["labels"] != ["DRAW" if winner is None else "WIN" if owner == winner else "LOSS" for owner in (0, 1)]
            or winner is None and evidence["crownsRaw"][0] != evidence["crownsRaw"][1]):
        raise ValueError("action random native terminal evidence differs")
    if ({entry["owner"] for entry in decisions} != {0, 1}
            or len({(entry["owner"], entry["tick"]) for entry in decisions}) != len(decisions)
            or any(not native["start_tick"] <= entry["tick"] < native["final_tick"] for entry in decisions)):
        raise ValueError("action random decision coverage differs")
    if [entry["threshold"] for entry in native["stages"]] != list(THRESHOLDS):
        raise ValueError("action random stage coverage differs")
    by_tick = {sample["tick"]: sample for sample in samples}
    for stage in native["stages"]:
        sample = stage["sample"]
        if (sample["tick"] != stage["threshold"] or sample != by_tick[sample["tick"]]
                or semantic_census(sample) != semantic_census(stage["restored"])
                or stage["before_digest"] != stage["after_digest"] or stage["before_digest"] != stage["restored_digest"]
                or stage["raw_unchanged"] is not True or stage["snapshot_released"] is not True):
            raise ValueError("action random stage restoration differs")
        require_sha(stage["before_digest"])
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    result = validate_action_random(root, root.parents[2])
    print(json.dumps(dict(status="passed", ticks=result["report"]["sampled_ticks"],
        runtime_observations=result["report"]["runtime_observations"])))
