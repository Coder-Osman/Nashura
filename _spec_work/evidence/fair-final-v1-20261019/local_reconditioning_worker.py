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
    root, base, ports = Path(settings["root"]), Path(settings["base"]), settings["ports"]
    probe = Path(settings.get("engine_path",
        str(base / "project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so")))
    if hashlib.sha256(probe.read_bytes()).hexdigest() != settings["engine_sha256"]:
        raise ValueError("prior integration engine identity differs")
    environment = dict(os.environ, CR_PROBE=str(probe), PYTHONUNBUFFERED="1")
    engines = []
    child, error, code = None, None, None
    try:
        for port in ports:
            instance = root / f"engine-{port}"
            launcher = base/"isolated_engine.py"
            extra = []
            if "engine_path" in settings:
                launcher = root/"project/phase_snapshot_launcher.py"
                extra = ["--probe", str(probe), "--engine-sha256", settings["engine_sha256"]]
            with (root / f"engine-{port}.log").open("a") as log:
                engine = subprocess.Popen([sys.executable, str(launcher),
                    "--project", str(base/"project"), "--port", str(port), "--instance", str(instance), *extra],
                    stdout=log, stderr=subprocess.STDOUT, env=environment)
            engines.append((port, engine, instance))
            wait_for_engine(engine, instance, timeout=120)
        command = [sys.executable, str(root/"project/tools/probe_fair_final_local_reconditioning.py"),
            "--port", str(ports[0]), "--belief-port", str(ports[1]), "--source", str(base/"candidate.pt"),
            "--prior", str(root/"public-deck-prior-engineering.json"),
            "--puct-root", str(base/"project/research/puct-optimized"), "--output", str(root/"report.json")]
        with (root/"probe.log").open("a") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
            started = time.monotonic()
            while child.poll() is None:
                available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:"))) * 1024
                if available < 32*1024**3 or shutil.disk_usage(root).free < 5*1024**3:
                    raise RuntimeError("prior integration resource reserve lost")
                if time.monotonic()-started > 1650:
                    raise TimeoutError("prior integration external deadline")
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
            engine.terminate()
            try:
                engine.wait(timeout=25)
            except subprocess.TimeoutExpired:
                engine.kill()
                engine.wait()
            path = instance/"process.json"
            process = json.loads(path.read_text()) if path.exists() else dict(phase="missing")
            alive = {key:process.get(key) is not None and Path(f"/proc/{process[key]}").exists()
                for key in ("pid", "child_pid")}
            with socket.socket() as check:
                listening = check.connect_ex(("127.0.0.1", port)) == 0
            cleanup.append(dict(port=port, process=process, pids_alive=alive, listening=listening))
        clean = len(cleanup) == len(engines) and all(not entry["listening"] and not any(entry["pids_alive"].values())
            and entry["process"]["phase"] in {"stopped", "exited", "failed"} for entry in cleanup)
        result = dict(exit_code=code, error=error, clean=clean, engines=cleanup)
        (root/"cleanup.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    raise SystemExit(int(code != 0 or error is not None or not clean))


if __name__ == "__main__":
    main()
