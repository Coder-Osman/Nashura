import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    root, project, port = Path(settings["root"]), Path(settings["project"]), settings["port"]
    environment = dict(os.environ, PYTHONPATH=str(project), OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
        CR_PROBE=settings["probe"])
    engine, code, error = None, None, None
    instance = root / "engine"
    try:
        with (root / "engine.log").open("x") as log:
            engine = subprocess.Popen([sys.executable, str(project / "phase_snapshot_launcher.py"),
                "--project", settings["base_project"], "--port", str(port),
                "--instance", str(instance), "--probe", settings["probe"],
                "--engine-sha256", settings["engine_sha256"]], stdout=log, stderr=subprocess.STDOUT, env=environment)
        wait_for_engine(engine, instance, timeout=120)
        process = json.loads((instance / "process.json").read_text())
        libraries = {line.split(maxsplit=5)[5] for line in Path(f"/proc/{process['child_pid']}/maps").read_text().splitlines()
            if len(line.split(maxsplit=5)) == 6 and line.endswith("/libg.so")}
        if len(libraries) != 1:
            raise ValueError("mode probe mapped library ambiguous")
        command = [sys.executable, str(root / "probe_mode.py"), "--instance", str(instance),
            "--probe", settings["probe"], "--libg", next(iter(libraries)), "--port", str(port),
            "--project", str(project), "--puct-root", settings["puct_root"],
            "--output", str(root / "report.json")]
        with (root / "probe.log").open("x") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
            try:
                code = child.wait(timeout=360)
            except BaseException:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise
    except BaseException as failure:
        error = repr(failure)
    finally:
        if engine is not None:
            engine.terminate()
            try:
                engine.wait(timeout=25)
            except subprocess.TimeoutExpired:
                engine.kill()
                engine.wait()
        path = instance / "process.json"
        process = json.loads(path.read_text()) if path.exists() else dict(phase="missing")
        alive = {name: process.get(name) is not None and Path(f"/proc/{process[name]}").exists()
            for name in ("pid", "child_pid")}
        with socket.socket() as connection:
            listening = connection.connect_ex(("127.0.0.1", port)) == 0
        clean = not listening and not any(alive.values()) and process["phase"] == "stopped"
        result = dict(exit_code=code, error=error, clean=clean, process=process,
            pids_alive=alive, listening=listening, port=port)
        (root / "cleanup.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    raise SystemExit(int(code != 0 or error is not None or not clean))


if __name__ == "__main__":
    main()
