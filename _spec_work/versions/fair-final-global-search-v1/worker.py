import json
import os
from pathlib import Path
import signal
import sys
import time

from selected_collection_worker import check_port, clean_engine, process_identity, run_phase, sha, write
from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def run(settings):
    root = Path(settings["root"])
    project = root / "project"
    for name, expected in settings["payload_sha256"].items():
        if sha(root / name) != expected:
            raise ValueError("global search uploaded payload differs")
    memory = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:"))) * 1024
    if memory < 32 * 1024**3 or os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < 5 * 1024**3:
        raise ValueError("global search resource floor failed")
    environment = dict(os.environ, PYTHONPATH=str(project), PYTHONDONTWRITEBYTECODE="1",
        CR_PROBE=str(root / "libcrprobe.so"), OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    engines = []
    job = root / "tick-110-seat-0"
    job.mkdir()
    started = time.monotonic()
    try:
        import subprocess
        for port in settings["ports"]:
            check_port(port)
            instance = job / ("engine-" + str(port))
            command = [sys.executable, "-B", str(project / "phase_snapshot_launcher.py"),
                "--project", settings["base"] + "/project", "--port", str(port),
                "--instance", str(instance), "--probe", str(root / "libcrprobe.so"),
                "--engine-sha256", settings["engine_sha256"]]
            with (job / ("engine-" + str(port) + ".log")).open("x") as log:
                engine = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                    env=environment, cwd=project)
            identity = process_identity(engine)
            engines.append((port, engine, instance, identity))
            wait_for_engine(engine, instance, timeout=180)
        command = [sys.executable, "-B", "-u", str(project / "probe_search.py"),
            "--port", str(settings["ports"][0]), "--belief-port", str(settings["ports"][1]),
            "--source", settings["base"] + "/candidate.pt",
            "--prior", str(project / "public-deck-prior-engineering.json"),
            "--trace", str(project / "input-trace.json"),
            "--puct-root", settings["base"] + "/project/research/puct-optimized",
            "--tick", "110", "--owner", "0", "--output", str(job / "report.json")]
        run_phase(job, "probe", command, timeout=5500, environment=environment)
    finally:
        cleanup = []
        for port, engine, instance, identity in reversed(engines):
            try:
                result = clean_engine(engine, instance, port, expected_identity=identity)
            except BaseException as error:
                result = dict(clean=False, error=repr(error), launcher=identity)
            cleanup.append(dict(port=port, **result))
        write(job / "cleanup.json", dict(engines=cleanup,
            clean=len(cleanup) == 2 and all(row["clean"] for row in cleanup)))
    if len(cleanup) != 2 or not all(row["clean"] for row in cleanup):
        raise ValueError("global search owned cleanup failed")
    write(root / "completion.json", dict(status="completed_requires_independent_verification",
        seconds=time.monotonic() - started, training_admissible=False, gates_closed=False))


def main():
    def interrupt(*unused):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    settings = json.loads(Path(sys.argv[1]).read_text())
    try:
        run(settings)
    except BaseException as error:
        write(Path(settings["root"]) / "failure.json", dict(error=repr(error),
            training_admissible=False, gates_closed=False))
        raise


if __name__ == "__main__":
    main()
