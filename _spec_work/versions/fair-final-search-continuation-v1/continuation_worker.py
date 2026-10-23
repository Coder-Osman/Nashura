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


def validate_phase_bound(settings):
    if (type(settings["wall_seconds"]) is not int or settings["wall_seconds"] not in (21600, 86400)
            or type(settings["segment_reserve_seconds"]) is not int
            or settings["segment_reserve_seconds"] != 1800):
        raise ValueError("continuation requires an approved six-hour or 24-hour bound and 30-minute reserve")


def main():
    root = Path(sys.argv[1])
    settings = json.loads((root / "settings.json").read_text())
    base = Path(settings["base"])
    for relative, expected in settings["driver_sha256"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("continuation driver or receipt identity differs")
    validate_phase_bound(settings)
    engine_path = root / "libcrprobe.so"
    if hashlib.sha256(engine_path.read_bytes()).hexdigest() != settings["engine_sha256"]:
        raise ValueError("complete search engine identity differs")
    manifest = json.loads((root / "project/probe-source-manifest.json").read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((root / "project" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("complete search deployed code differs")
    environment = dict(os.environ,CR_PROBE=str(engine_path),OMP_NUM_THREADS="1",MKL_NUM_THREADS="1")
    engines = []
    child, error = None, None
    phases = []
    try:
        for port in settings["ports"]:
            instance = root / f"engine-{port}"
            with (root / f"engine-{port}.log").open("a") as log:
                engine = subprocess.Popen([sys.executable,str(root / "project/phase_snapshot_launcher.py"),
                    "--project",str(base / "project"),"--port",str(port),"--instance",str(instance),
                    "--probe",str(engine_path),"--engine-sha256",settings["engine_sha256"]],
                    env=environment,stdout=log,stderr=subprocess.STDOUT)
            engines.append((port,engine,instance))
            wait_for_engine(engine,instance,timeout=120)
        for phase in ("continue",):
            command = [sys.executable,"-u",str(root / "project/tools/probe_search_continuation.py"),
                "--port",str(settings["ports"][0]),"--belief-port",str(settings["ports"][1]),
                "--source",str(base / "candidate.pt"),"--prior",str(root / "project/prior.json"),
                "--puct-root",str(base / "project/research/puct-optimized"),
                "--output",str(root / "episode"),"--engine-sha256",settings["engine_sha256"],
                "--phase",phase,"--wall-seconds",str(settings["wall_seconds"]),
                "--continuation-input",str(root / "continuation-input.json"),
                "--segment-reserve-seconds",str(settings["segment_reserve_seconds"])]
            with (root / (phase + ".log")).open("a") as log:
                child = subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=environment)
            started = time.monotonic()
            while child.poll() is None:
                available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:")))*1024
                if available < 32*1024**3 or shutil.disk_usage(root).free < 5*1024**3:
                    raise RuntimeError("engineering resource reserve lost")
                if time.monotonic()-started > settings["wall_seconds"]+30:
                    raise TimeoutError("complete search phase deadline")
                time.sleep(3)
            phases.append(dict(phase=phase,exit_code=child.returncode))
            (root / "phase-results.json").write_text(json.dumps(phases,indent=2))
            if child.returncode:
                raise RuntimeError(f"complete search phase {phase} failed")
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
        for port,engine,instance in engines:
            engine.terminate()
            try:
                engine.wait(timeout=25)
            except subprocess.TimeoutExpired:
                engine.kill()
                engine.wait()
            process = json.loads((instance / "process.json").read_text())
            alive = {name:process.get(name) is not None and Path(f"/proc/{process[name]}").exists()
                for name in ("pid","child_pid")}
            with socket.socket() as check:
                listening = check.connect_ex(("127.0.0.1",port)) == 0
            cleanup.append(dict(port=port,process=process,pids_alive=alive,listening=listening))
        clean = len(cleanup)==2 and all(not row["listening"] and not any(row["pids_alive"].values())
            and row["process"]["phase"]=="stopped" for row in cleanup)
        checkpointed = False
        if (root / "episode/segment-report.json").exists():
            segment = json.loads((root / "episode/segment-report.json").read_text())
            checkpointed = segment.get("status") == "checkpointed" and segment.get("training_admissible") is False
        result = dict(error=error,phases=phases,clean=clean,engines=cleanup,checkpointed=checkpointed,
            training_admissible=False,complete=(root / "episode/complete-report.json").exists())
        (root / "completion.json").write_text(json.dumps(result,indent=2))
        print(json.dumps(dict(error=error,clean=clean,complete=result["complete"])),flush=True)
    raise SystemExit(int(error is not None or not clean or not (result["complete"] or checkpointed)))


if __name__ == "__main__":
    main()

