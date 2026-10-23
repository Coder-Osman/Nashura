import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from prior_sampling import sha
from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def run(settings, variant):
    root, project = Path(settings["root"]), Path(settings["project"])
    output = root / variant
    output.mkdir()
    instance = output / "engine"
    environment = dict(os.environ, PYTHONPATH=str(project), OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
        CR_PROBE=settings["probe"])
    engine, child, code, error = None, None, None, None
    try:
        with (output / "engine.log").open("x") as log:
            engine = subprocess.Popen([sys.executable, str(project / "phase_snapshot_launcher.py"),
                "--project", settings["base_project"], "--port", str(settings["port"]),
                "--instance", str(instance), "--probe", settings["probe"],
                "--engine-sha256", settings["engine_sha256"]], stdout=log, stderr=subprocess.STDOUT, env=environment)
        wait_for_engine(engine, instance, timeout=120)
        with (output / "probe.log").open("x") as log:
            child = subprocess.Popen([sys.executable, "-u", str(root / "probe_openings.py"),
                str(root / "settings.json"), variant], stdout=log, stderr=subprocess.STDOUT, env=environment)
            code = child.wait(timeout=settings["timeout_seconds"])
    except BaseException as failure:
        error = repr(failure)
    finally:
        for process in (child, engine):
            if process is None or process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process_path = instance / "process.json"
        process = json.loads(process_path.read_text()) if process_path.exists() else dict(phase="missing")
        alive = {name: process.get(name) is not None and Path(f"/proc/{process[name]}").exists()
            for name in ("pid", "child_pid")}
        with socket.socket() as connection:
            listening = connection.connect_ex(("127.0.0.1", settings["port"])) == 0
        clean = not listening and not any(alive.values()) and process["phase"] == "stopped"
        result = dict(exit_code=code, error=error, clean=clean, process=process,
            pids_alive=alive, listening=listening, port=settings["port"])
        (output / "cleanup.json").write_text(json.dumps(result, indent=2) + "\n")
    return code == 0 and error is None and clean


def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    for name, expected in settings["source_sha256"].items():
        if sha(Path(settings["root"]) / name) != expected:
            raise ValueError("opening worker source identity differs")
    for variant in ("primary", "repeat"):
        if not run(settings, variant):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
