import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2]/"firstlight_CR"))

from native_runner.training.fair_final.episode import atomic_json


def completion_summary(report):
    records = [entry for shard in report["shards"] for entry in shard["report"]["records"]]
    stages = []
    for tick in (110, 2598, 5602):
        subset = [entry for shard in report["shards"] if shard["job"]["tick"] == tick
            for entry in shard["report"]["records"]]
        cold = [entry for entry in subset if entry["stage"] == "initial" and not entry["altered"]]
        reused = [entry for entry in subset if entry["altered"]]
        successors = [entry for entry in subset if entry["stage"] == "successor" and not entry["altered"]]
        stages.append(dict(tick=tick,
            cold_belief_seconds_median=statistics.median(entry["audit"]["timings"]["belief"] for entry in cold),
            reused_belief_seconds_median=statistics.median(entry["audit"]["timings"]["belief"] for entry in reused),
            successor_belief_seconds_median=statistics.median(entry["audit"]["timings"]["belief"] for entry in successors),
            local_reconditioning_seconds_median=statistics.median(entry["local_profile"]["seconds"] for entry in subset),
            search_seconds_median=statistics.median(entry["audit"]["seconds"] for entry in subset),
            successor_delta_ticks=[entry["local_profile"]["anchor_event"]["to_tick"]-
                entry["local_profile"]["anchor_event"]["from_tick"] for entry in successors]))
    return dict(schema="fair-final-refill-provider-completion.v1", status="passed", training_admissible=False,
        roots=len(records), effective_simulations=sum(entry["outcome"]["result"]["valid_simulations"] for entry in records),
        invalid_attempts=sum(entry["outcome"]["result"]["invalid_attempts"] for entry in records),
        root_restores=sum(entry["world_lifecycle"]["restores"] for entry in records),
        phase_restores=sum(entry["world_lifecycle"]["phase_restores"] for entry in records),
        hidden_pairs=len(records)//2, stages=stages, clean=all(shard["cleanup"]["clean"] for shard in report["shards"]),
        report_sha256=hashlib.sha256((ROOT/"native-refill-provider-report.json").read_bytes()).hexdigest(),
        exclusions=report["shards"][0]["report"]["exclusions"])


def finish(attempt_directory, *, timeout_seconds=6000):
    attempt_directory = attempt_directory.resolve()
    if (attempt_directory.parent != ROOT or not attempt_directory.name.startswith("native-refill-provider-")
            or not 0 < timeout_seconds <= 7200):
        raise ValueError("finalizer requires a registered refill provider attempt and bounded timeout")
    finalization_directory = ROOT/(attempt_directory.name+"-finalization")
    if finalization_directory.exists():
        finalization_directory = ROOT/(attempt_directory.name+f"-finalization-retry-{time.time_ns()}")
    finalization_directory.mkdir()
    status_path = finalization_directory/"status.json"
    started = time.monotonic()
    expected = json.loads((attempt_directory/"attempt.json").read_text())
    status = dict(schema="fair-final-refill-provider-finalization.v1", status="waiting",
        training_admissible=False, remote=expected["remote"], timeout_seconds=timeout_seconds, steps=[])
    atomic_json(status_path, status)
    try:
        while True:
            if time.monotonic()-started >= timeout_seconds:
                raise TimeoutError("refill provider finalization deadline")
            try:
                attempt = json.loads((attempt_directory/"attempt.json").read_text())
            except json.JSONDecodeError:
                time.sleep(5)
                continue
            if any(attempt.get(key) != value for key, value in expected.items() if key not in ("exit_code", "stdout", "stderr")):
                raise ValueError("refill provider attempt identity changed")
            if "exit_code" in attempt:
                if attempt["exit_code"] != 0:
                    raise RuntimeError("native refill provider failed; inspect collected shard logs")
                path = ROOT/"native-refill-provider-report.json"
                if path.exists():
                    try:
                        report = json.loads(path.read_text())
                    except json.JSONDecodeError:
                        time.sleep(5)
                        continue
                    if report["remote"] == expected["remote"]:
                        break
            time.sleep(5)
        status["status"] = "validating"
        atomic_json(status_path, status)
        for name in ("verify_refill_provider.py", "test_refill_provider_evidence.py", "build_gate_report.py"):
            with (finalization_directory/(name+".log")).open("w") as log:
                result = subprocess.run([sys.executable, str(ROOT/name)], cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT, timeout=240)
            status["steps"].append(dict(script=name, exit_code=result.returncode))
            atomic_json(status_path, status)
            if result.returncode:
                raise RuntimeError("refill provider finalization failed: "+name)
        summary = completion_summary(report)
        atomic_json(finalization_directory/"completion-summary.json", summary)
        status.update(status="passed", completed=time.time(), seconds=time.monotonic()-started,
            summary="completion-summary.json")
        atomic_json(status_path, status)
        print(json.dumps(summary), flush=True)
    except BaseException as error:
        status.update(status="failed", error=repr(error), seconds=time.monotonic()-started)
        atomic_json(status_path, status)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt_directory", type=Path)
    arguments = parser.parse_args()
    finish(arguments.attempt_directory)
