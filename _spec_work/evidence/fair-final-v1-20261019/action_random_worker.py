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
    manifest = json.loads(Path(sys.argv[1]).read_text())
    root, base, port = Path(manifest["root"]), Path(manifest["base"]), manifest["port"]
    probe_root = base / "project/research/standalone-resident-p18-stepatomic"
    environment = dict(os.environ, CR_PROBE=str(probe_root / "attestation-history/libcrprobe.so"))
    instance = root / "engine"
    engine, child, error, code = None, None, None, None
    try:
        with (root / "engine.log").open("a") as log:
            engine = subprocess.Popen([sys.executable, str(base / "isolated_engine.py"),
                "--project", str(base / "project"), "--port", str(port), "--instance", str(instance)],
                stdout=log, stderr=subprocess.STDOUT)
        wait_for_engine(engine, instance, timeout=120)
        process = json.loads((instance / "process.json").read_text())
        maps = Path(f"/proc/{process['child_pid']}/maps").read_text()
        paths = {line.split(maxsplit=5)[5] for line in maps.splitlines()
            if len(line.split(maxsplit=5)) == 6 and line.endswith("/libg.so")}
        if len(paths) != 1:
            raise ValueError("action random mapped library ambiguous")
        command = [sys.executable, str(root / "project/tools/probe_fair_final_action_random.py"),
            "--port", str(port), "--instance", str(instance), "--probe", str(probe_root / "libresident.so"),
            "--libg", next(iter(paths)), "--source", str(base / "candidate.pt"),
            "--puct-root", str(base / "project/research/puct-optimized"), "--output", str(root / "report.json")]
        with (root / "probe.log").open("a") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
            started = time.monotonic()
            while child.poll() is None:
                available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:"))) * 1024
                if available < 32 * 1024**3:
                    raise RuntimeError("action random memory reserve lost")
                if time.monotonic() - started > 1260:
                    raise TimeoutError("action random external deadline")
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
        path = instance / "process.json"
        process = json.loads(path.read_text()) if path.exists() else {}
        child_pid = process.get("child_pid")
        if child_pid and Path(f"/proc/{child_pid}/stat").exists():
            start = int(Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
            if start == int(process["child_start_ticks"]):
                os.kill(child_pid, signal.SIGCONT)
        if engine is not None:
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
        clean = not listening and not any(alive.values()) and process["phase"] == "stopped"
        result = dict(exit_code=code, error=error, clean=clean, process=process,
            pids_alive=alive, listening=listening, port=port)
        (root / "cleanup.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    raise SystemExit(int(code != 0 or error is not None or not clean))


if __name__ == "__main__":
    main()

