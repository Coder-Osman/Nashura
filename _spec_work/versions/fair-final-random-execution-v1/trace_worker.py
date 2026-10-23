import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from selected_collection_worker import check_port, clean_engine, process_identity, run_phase, write
from trace_protocol import sha, summarize
from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def probe_environment(settings):
    return dict(os.environ, PYTHONPATH=settings["project"], PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", CR_PROBE=settings["probe"])


def run(settings, mode, *, center=None):
    root = Path(settings["root"])
    output = root / mode
    output.mkdir(exist_ok=False)
    port = settings["port"]
    check_port(port)
    memory = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:"))) * 1024
    if memory < 32 * 1024**3 or os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < 5 * 1024**3:
        raise ValueError("execution trace resource floor failed")
    controls = ("QEMU_LOG", "QEMU_DFILTER", "QEMU_LOG_FILENAME", "QEMU_GUEST_BASE", "QEMU_RESERVED_VA",
        "QEMU_SINGLESTEP", "QEMU_ONE_INSN_PER_TB")
    if any(os.environ.get(name) for name in controls):
        raise ValueError("inherited QEMU execution controls differ")
    configuration = dict(mode=mode, center_base=center, qemu_sha256=settings["qemu_sha256"],
        service_sha256=settings["service_sha256"])
    config_path = output / "trace-settings.json"
    write(config_path, configuration)
    environment = probe_environment(settings)
    instance = output / "engine"
    command = [sys.executable, "-B", "-u", str(root / "trace_launcher.py"),
        "--project", settings["base_project"], "--port", str(port), "--instance", str(instance),
        "--probe", settings["probe"], "--engine-sha256", settings["engine_sha256"],
        "--trace-settings", str(config_path)]
    engine, identity, cleanup = None, None, None
    try:
        with (output / "engine.log").open("x") as log:
            engine = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                cwd=root, env=environment)
            identity = process_identity(engine)
            write(output / "engine-owner.json", dict(process=identity, command=command))
            wait_for_engine(engine, instance, timeout=660)
            state = json.loads((instance / "process.json").read_text())
            attestation = state.get("random_execution_trace", {})
            from trace_launcher import service_source

            _, service_sha = service_source(Path(settings["base_project"]) /
                "research/standalone-resident-p18-stepatomic/service.py")
            if (state.get("pid") != identity["pid"] or state.get("start_ticks") != identity["start_ticks"]
                    or attestation != dict(settings_sha256=sha(config_path),
                        launcher_sha256=sha(root / "trace_launcher.py"),
                        protocol_sha256=sha(root / "trace_protocol.py"),
                        startup_deadline_seconds=600, startup_service_sha256=service_sha)):
                raise ValueError("execution trace ready attestation differs")
            maps = Path("/proc", str(state["child_pid"]), "maps").read_text()
            libraries = {line.split(maxsplit=5)[5] for line in maps.splitlines()
                if len(line.split(maxsplit=5)) == 6 and line.endswith("/libg.so")}
            if len(libraries) != 1:
                raise ValueError("execution trace library mapping ambiguous")
            arguments = [sys.executable, "-B", "-u", str(root / "probe_trace.py"),
                "--instance", str(instance), "--probe", settings["probe"], "--libg", libraries.pop(),
                "--port", str(port), "--project", settings["project"], "--puct-root", settings["puct_root"],
                "--output", str(output / "report.json"), "--mode", mode]
            if center is not None:
                arguments += ["--center-base", str(center)]
            run_phase(output, "probe", arguments, timeout=960, environment=environment)
    finally:
        if engine is not None:
            cleanup = clean_engine(engine, instance, port, expected_identity=identity)
            write(output / "cleanup.json", cleanup)
    if cleanup is None or not cleanup["clean"]:
        raise ValueError("execution trace owned engine cleanup failed")
    report = json.loads((output / "report.json").read_text())
    if mode == "trace":
        opening = report["opening"]
        summary = summarize(output / "qemu.log", base=opening["libg_base"],
            reset_offset=report["reset_offset"], battle_offset=report["battle_offset"],
            final_offset=report["final_offset"], global_object=opening["libg_base"] + 0x19FC368,
            manager_object=opening["manager"] + 0xA0)
        write(output / "trace-summary.json", summary)
    return report


def main():
    def interrupt(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    settings_path = Path(sys.argv[1])
    settings = json.loads(settings_path.read_text())
    root = Path(settings["root"])
    for name, digest in settings["source_sha256"].items():
        if sha(root / name) != digest:
            raise ValueError("execution trace uploaded source differs")
    started = time.monotonic()
    try:
        calibrated = run(settings, "calibration")
        center = calibrated["opening"]["libg_base"]
        run(settings, "trace", center=center)
        write(root / "completion.json", dict(status="completed_requires_independent_verification",
            settings_sha256=sha(settings_path), seconds=time.monotonic() - started,
            training_admissible=False, gates_closed=False))
    except BaseException as error:
        write(root / "failure.json", dict(error=repr(error), seconds=time.monotonic() - started,
            training_admissible=False, gates_closed=False))
        raise


if __name__ == "__main__":
    main()
