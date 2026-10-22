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
    rows = [shard["report"] for shard in report["shards"]]
    return dict(schema="fair-final-refill-search-completion.v1", status="passed", training_admissible=False,
        roots=len(rows), effective_simulations=sum(row["outcome"]["result"]["valid_simulations"] for row in rows),
        restores=sum(row["world_lifecycle"]["restores"] for row in rows),
        clean=all(shard["cleanup"]["clean"] for shard in report["shards"]),
        report_sha256=hashlib.sha256((ROOT/"native-refill-search-report.json").read_bytes()).hexdigest(),
        exclusions=["local-refill-reconditioning", "hidden-state-intervention-pairs", "complete-game-search", "training-admission"])


def finish(attempt_directory, *, timeout_seconds=4200):
    attempt_directory = attempt_directory.resolve()
    if attempt_directory.parent != ROOT or not attempt_directory.name.startswith("native-refill-search-"):
        raise ValueError("finalizer requires a registered refill search attempt")
    finalization_directory = ROOT/(attempt_directory.name+"-finalization")
    if finalization_directory.exists():
        finalization_directory = ROOT/(attempt_directory.name+"-finalization-retry-"+str(time.time_ns()))
    finalization_directory.mkdir()
    status_path = finalization_directory/"status.json"
    started = time.monotonic()
    expected = json.loads((attempt_directory/"attempt.json").read_text())
    expected_remote = expected["remote"]
    status = dict(schema="fair-final-refill-search-finalization.v1", status="waiting",
        training_admissible=False, remote=expected_remote, timeout_seconds=timeout_seconds, steps=[])
    atomic_json(status_path, status)
    try:
        while True:
            if time.monotonic()-started >= timeout_seconds:
                raise TimeoutError("refill search finalization deadline")
            attempt = json.loads((attempt_directory/"attempt.json").read_text())
            if attempt["remote"] != expected_remote:
                raise ValueError("local attempt identity changed")
            if "exit_code" in attempt:
                if attempt["exit_code"] != 0:
                    raise RuntimeError("native refill search failed; inspect collected shard logs")
                path = ROOT/"native-refill-search-report.json"
                if path.exists():
                    report = json.loads(path.read_text())
                    if report["remote"] == expected_remote:
                        break
            time.sleep(5)
        status["status"] = "validating"
        atomic_json(status_path, status)
        for name, arguments in (("verify_refill.py", ["--search"]), ("test_refill_evidence.py", []),
                ("test_refill_evidence.py", ["--search"]), ("build_gate_report.py", [])):
            log_name = name+("-search" if arguments else "")+".log"
            with (finalization_directory/log_name).open("w") as log:
                result = subprocess.run([sys.executable, str(ROOT/name), *arguments], cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT, timeout=180)
            status["steps"].append(dict(script=name, arguments=arguments, exit_code=result.returncode))
            atomic_json(status_path, status)
            if result.returncode:
                raise RuntimeError("refill search finalization failed: "+name)
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
