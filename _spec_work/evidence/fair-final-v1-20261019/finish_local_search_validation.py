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
    records = [dict(job=shard["job"]["name"], **entry)
        for shard in report["shards"] for entry in shard["report"]["records"]]
    stages = []
    for threshold in (1200, 3600):
        subset = [entry for entry in records if entry["threshold"] == threshold]
        cold = [entry for entry in subset if entry["stage"] == "initial" and not entry["altered"]]
        reused = [entry for entry in subset if entry["altered"]]
        successors = [entry for entry in subset if entry["stage"] == "successor" and not entry["altered"]]
        stages.append(dict(threshold=threshold,
            cold_belief_seconds_median=statistics.median(entry["audit"]["timings"]["belief"] for entry in cold),
            reused_belief_seconds_median=statistics.median(entry["audit"]["timings"]["belief"] for entry in reused),
            successor_belief_seconds_median=statistics.median(entry["audit"]["timings"]["belief"] for entry in successors),
            successor_delta_ticks=[entry["local_profile"]["anchor_event"]["to_tick"]-
                entry["local_profile"]["anchor_event"]["from_tick"] for entry in successors],
            local_reconditioning_seconds_median=statistics.median(entry["local_profile"]["seconds"] for entry in subset)))
    return dict(schema="fair-final-local-search-completion.v1", status="passed", training_admissible=False,
        roots=len(records), effective_simulations=sum(entry["outcome"]["result"]["valid_simulations"] for entry in records),
        hidden_pairs=len(records)//2, stages=stages, clean=all(shard["cleanup"]["clean"] for shard in report["shards"]),
        report_sha256=hashlib.sha256((ROOT/"native-local-search-report.json").read_bytes()).hexdigest(),
        exclusions=["normal-single-ready-root", "pending-and-incomplete-refill", "multi-slot",
            "full-game-quality-and-throughput", "constant-total-nonsearch-overhead", "training-admission"])


def finish(attempt_directory, *, timeout_seconds=4200):
    attempt_directory = attempt_directory.resolve()
    if attempt_directory.parent != ROOT or not attempt_directory.name.startswith("native-local-search-"):
        raise ValueError("finalizer requires a registered local search attempt")
    finalization_directory = ROOT/(attempt_directory.name+"-finalization")
    finalization_directory.mkdir(exist_ok=True)
    status_path = finalization_directory/"status.json"
    started = time.monotonic()
    expected = json.loads((attempt_directory/"attempt.json").read_text())
    expected_remote = expected["remote"]
    status = dict(schema="fair-final-local-search-finalization.v1", status="waiting",
        training_admissible=False, remote=expected_remote, timeout_seconds=timeout_seconds, steps=[])
    atomic_json(status_path, status)
    try:
        while True:
            if time.monotonic()-started >= timeout_seconds:
                raise TimeoutError("local search finalization deadline")
            attempt = json.loads((attempt_directory/"attempt.json").read_text())
            if attempt["remote"] != expected_remote:
                raise ValueError("local attempt identity changed")
            if "exit_code" in attempt:
                if attempt["exit_code"] != 0:
                    raise RuntimeError("native local search failed; inspect collected shard logs")
                path = ROOT/"native-local-search-report.json"
                if path.exists():
                    report = json.loads(path.read_text())
                    if report["remote"] == expected_remote:
                        break
            time.sleep(5)
        status["status"] = "validating"
        atomic_json(status_path, status)
        for name in ("verify_local_search.py", "test_local_search_evidence.py", "build_gate_report.py"):
            with (finalization_directory/(name+".log")).open("w") as log:
                result = subprocess.run([sys.executable, str(ROOT/name)], cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT, timeout=180)
            status["steps"].append(dict(script=name, exit_code=result.returncode))
            atomic_json(status_path, status)
            if result.returncode:
                raise RuntimeError("local search finalization failed: "+name)
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
