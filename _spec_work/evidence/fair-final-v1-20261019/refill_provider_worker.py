import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def run_job(settings, job):
    root, base = Path(settings["root"]), Path(settings["base"])
    directory = root/job["name"]
    directory.mkdir()
    probe = Path(settings["engine_path"])
    if hashlib.sha256(probe.read_bytes()).hexdigest() != settings["engine_sha256"]:
        raise ValueError("refill provider engine identity differs")
    environment = dict(os.environ, CR_PROBE=str(probe), PYTHONUNBUFFERED="1",
        OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    engines = []
    child, error, code = None, None, None
    try:
        for port in job["ports"]:
            instance = directory/f"engine-{port}"
            with (directory/f"engine-{port}.log").open("a") as log:
                engine = subprocess.Popen([sys.executable, str(root/"project/phase_snapshot_launcher.py"),
                    "--project", str(base/"project"), "--port", str(port), "--instance", str(instance),
                    "--probe", str(probe), "--engine-sha256", settings["engine_sha256"]],
                    stdout=log, stderr=subprocess.STDOUT, env=environment)
            engines.append((port, engine, instance))
            wait_for_engine(engine, instance, timeout=120)
        command = [sys.executable, str(root/"project/tools/probe_fair_final_refill_provider.py"),
            "--port", str(job["ports"][0]), "--belief-port", str(job["ports"][1]),
            "--source", str(base/"candidate.pt"),
            "--prior", str(root/"project/public-deck-prior-engineering.json"),
            "--trace", str(root/"project/input-trace.json"),
            "--puct-root", str(base/"project/research/puct-optimized"),
            "--tick", str(job["tick"]), "--owner", str(job["owner"]),
            "--output", str(directory/"report.json")]
        with (directory/"probe.log").open("a") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
            started = time.monotonic()
            while child.poll() is None:
                available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:")))*1024
                if available < 32*1024**3 or shutil.disk_usage(root).free < 5*1024**3:
                    raise RuntimeError("refill provider resource reserve lost")
                if time.monotonic()-started > 5500:
                    raise TimeoutError("refill provider worker deadline")
                time.sleep(3)
            code = child.returncode
    except BaseException as failure:
        error = repr(failure)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        cleanup = []
        for port, engine, instance in engines:
            engine.terminate()
            try:
                engine.wait(timeout=25)
            except subprocess.TimeoutExpired:
                engine.kill()
                engine.wait()
            path = instance/"process.json"
            process = json.loads(path.read_text()) if path.exists() else dict(phase="missing")
            alive = {key: process.get(key) is not None and Path(f"/proc/{process[key]}").exists()
                for key in ("pid", "child_pid")}
            with socket.socket() as check:
                listening = check.connect_ex(("127.0.0.1", port)) == 0
            cleanup.append(dict(port=port, process=process, pids_alive=alive, listening=listening))
        clean = len(cleanup) == 2 and all(not row["listening"] and not any(row["pids_alive"].values())
            and row["process"]["phase"] == "stopped" for row in cleanup)
        result = dict(job=job, exit_code=code, error=error, clean=clean, engines=cleanup)
        (directory/"cleanup.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda job: run_job(settings, job), settings["jobs"]))
    (Path(settings["root"])/"run-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results), flush=True)
    raise SystemExit(int(any(row["exit_code"] != 0 or row["error"] is not None or not row["clean"]
        for row in results)))


if __name__ == "__main__":
    main()
