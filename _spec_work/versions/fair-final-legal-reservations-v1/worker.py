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


def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    root, base = Path(settings["root"]), Path(settings["base"])
    probe = root / "libcrprobe.so"
    if hashlib.sha256(probe.read_bytes()).hexdigest() != settings["engine_sha256"]:
        raise ValueError("legal probe engine identity differs")
    port = settings["port"]
    instance = root / "engine"
    environment = dict(os.environ, CR_PROBE=str(probe), PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    engine, child, error, code = None, None, None, None
    try:
        with (root / "engine.log").open("a") as log:
            engine = subprocess.Popen([sys.executable, str(root / "project/phase_snapshot_launcher.py"),
                "--project", str(base / "project"), "--port", str(port), "--instance", str(instance),
                "--probe", str(probe), "--engine-sha256", settings["engine_sha256"]],
                stdout=log, stderr=subprocess.STDOUT, env=environment)
        wait_for_engine(engine, instance, timeout=120)
        with (root / "probe.log").open("a") as log:
            child = subprocess.Popen([sys.executable, str(root / "project/probe.py"), "--port", str(port),
                "--source", str(base / "candidate.pt"), "--trace", str(root / "project/input-trace.json"),
                "--puct-root", str(base / "project/research/puct-optimized"),
                "--output", str(root / "report.json")], stdout=log, stderr=subprocess.STDOUT, env=environment)
            started = time.monotonic()
            while child.poll() is None:
                available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:"))) * 1024
                if available < 32 * 1024**3 or shutil.disk_usage(root).free < 5 * 1024**3:
                    raise RuntimeError("legal probe diagnostic reserve lost")
                if time.monotonic() - started > 1260:
                    raise TimeoutError("legal probe worker deadline")
                time.sleep(2)
            code = child.returncode
    except BaseException as failure:
        error = repr(failure)
    finally:
        for process in (child, engine):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        process_file = instance / "process.json"
        process = json.loads(process_file.read_text()) if process_file.exists() else dict(phase="missing")
        alive = {key: process.get(key) is not None and Path(f"/proc/{process[key]}").exists()
            for key in ("pid", "child_pid")}
        with socket.socket() as check:
            check.settimeout(1)
            listening = check.connect_ex(("127.0.0.1", port)) == 0
        clean = not listening and not any(alive.values()) and process["phase"] == "stopped"
        result = dict(exit_code=code, error=error, clean=clean, port=port,
            process=process, pids_alive=alive, listening=listening)
        (root / "cleanup.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    raise SystemExit(int(code != 0 or error is not None or not clean))


if __name__ == "__main__":
    main()
