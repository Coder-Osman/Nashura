import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    root, base = Path(manifest["root"]), Path(manifest["base"])
    probe_root = base / "project/research/standalone-resident-p18-stepatomic"
    environment = dict(os.environ, CR_PROBE=str(probe_root / "attestation-history/libcrprobe.so"))

    def run_job(job):
        directory = root / job["name"]
        directory.mkdir()
        engines, child, error, code = [], None, None, None
        try:
            for port in job["ports"]:
                instance = directory / f"engine-{port}"
                with (directory / f"engine-{port}.log").open("a") as log:
                    engine = subprocess.Popen([sys.executable, str(base / "isolated_engine.py"),
                        "--project", str(base / "project"), "--port", str(port),
                        "--instance", str(instance)], stdout=log, stderr=subprocess.STDOUT)
                engines.append((port, engine, instance))
                wait_for_engine(engine, instance, timeout=120)
            process = json.loads((engines[0][2] / "process.json").read_text())
            maps = Path(f"/proc/{process['child_pid']}/maps").read_text()
            libg_paths = {line.split(maxsplit=5)[5] for line in maps.splitlines()
                if len(line.split(maxsplit=5)) == 6 and line.endswith("/libg.so")}
            if len(libg_paths) != 1:
                raise ValueError("one actual mapped libg required")
            command = [sys.executable, str(root / "project/tools/probe_fair_final_prng.py"),
                "--port", str(job["ports"][0]), "--belief-port", str(job["ports"][1]),
                "--instance", str(engines[0][2]), "--probe", str(probe_root / "libresident.so"),
                "--libg", next(iter(libg_paths)), "--source", str(base / "candidate.pt"),
                "--puct-root", str(base / "project/research/puct-optimized"),
                "--threshold", str(job["threshold"]), "--owner", str(job["owner"]),
                "--output", str(directory / "report.json")]
            if manifest["phase"] == "smoke":
                command.append("--smoke-only")
            with (directory / "probe.log").open("a") as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
                started = time.monotonic()
                while child.poll() is None:
                    available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                        if line.startswith("MemAvailable:"))) * 1024
                    if available < 32 * 1024**3:
                        raise RuntimeError("PRNG audit memory reserve lost")
                    if time.monotonic() - started > (240 if manifest["phase"] == "smoke" else 3600):
                        raise TimeoutError("bounded PRNG validation deadline")
                    time.sleep(2)
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
                path = instance / "process.json"
                process = json.loads(path.read_text()) if path.exists() else {}
                child_pid = process.get("child_pid")
                if child_pid and Path(f"/proc/{child_pid}/stat").exists():
                    start = int(Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
                    if start == int(process["child_start_ticks"]):
                        os.kill(child_pid, signal.SIGCONT)
                engine.terminate()
                try:
                    engine.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    engine.kill()
                    engine.wait()
                process = json.loads(path.read_text()) if path.exists() else {"phase": "missing"}
                alive = {key: process.get(key) is not None and Path(f"/proc/{process[key]}").exists()
                    for key in ("pid", "child_pid")}
                with socket.socket() as check:
                    listening = check.connect_ex(("127.0.0.1", port)) == 0
                cleanup.append(dict(port=port, process=process, pids_alive=alive, listening=listening))
            clean = len(cleanup) == 2 and all(not item["listening"] and not any(item["pids_alive"].values())
                and item["process"]["phase"] == "stopped" for item in cleanup)
            result = dict(job=job, exit_code=code, error=error, clean=clean, engines=cleanup)
            (directory / "cleanup.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(manifest["jobs"])) as pool:
        results = list(pool.map(run_job, manifest["jobs"]))
    (root / "run-results.json").write_text(json.dumps(results, indent=2))
    raise SystemExit(int(any(item["exit_code"] != 0 or item["error"] or not item["clean"] for item in results)))


if __name__ == "__main__":
    main()
