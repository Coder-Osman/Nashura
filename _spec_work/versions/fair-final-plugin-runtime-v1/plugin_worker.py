import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from plugin_protocol import check_base, sha, summarize
from selected_collection_worker import check_port, clean_engine, process_identity, run_phase, write
from trace_worker import probe_environment
from native_runner.training.fair_search.evaluate_policy import wait_for_engine


def run(settings, variant, *, center=None):
    root = Path(settings["root"])
    output = root / variant
    output.mkdir(exist_ok=False)
    port = settings["port"]
    check_port(port)
    memory = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:"))) * 1024
    if memory < 32 * 1024**3 or os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < 5 * 1024**3:
        raise ValueError("plugin execution resource floor failed")
    if any(os.environ.get(name) for name in ("QEMU_PLUGIN", "QEMU_LOG", "QEMU_DFILTER",
            "QEMU_LOG_FILENAME", "QEMU_GUEST_BASE", "QEMU_RESERVED_VA", "QEMU_SINGLESTEP",
            "QEMU_ONE_INSN_PER_TB")):
        raise ValueError("inherited QEMU execution controls differ")
    configuration = dict(settings, variant=variant,
        center_base=None if settings.get("arm_after_ready") else center)
    configuration_path = output / "plugin-settings.json"
    write(configuration_path, configuration)
    environment = probe_environment(settings)
    instance = output / "engine"
    command = [sys.executable, "-B", "-u", str(root / "plugin_launcher.py"),
        "--settings", str(configuration_path), "--port", str(port), "--instance", str(instance)]
    engine, identity, cleanup = None, None, None
    started = time.monotonic()
    try:
        with (output / "engine.log").open("x") as log:
            engine = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                cwd=root, env=environment)
            identity = process_identity(engine)
            write(output / "engine-owner.json", dict(process=identity, command=command))
            wait_for_engine(engine, instance, timeout=180)
            state = json.loads((instance / "process.json").read_text())
            if (state.get("pid") != identity["pid"] or state.get("start_ticks") != identity["start_ticks"]
                    or state.get("random_plugin_trace") != dict(variant=variant,
                        settings_sha256=sha(configuration_path), launcher_sha256=sha(root / "plugin_launcher.py"),
                        qemu_sha256=settings["qemu_sha256"] if variant == "baseline" else settings["plugin_qemu_sha256"],
                        plugin_sha256=settings["plugin_sha256"] if variant == "plugin" else None)):
                raise ValueError("plugin native ready attestation differs")
            maps = Path("/proc", str(state["child_pid"]), "maps").read_text()
            libraries = {line.split(maxsplit=5)[5] for line in maps.splitlines()
                if len(line.split(maxsplit=5)) == 6 and line.endswith("/libg.so")}
            if len(libraries) != 1:
                raise ValueError("plugin library mapping ambiguous")
            library = libraries.pop()
            from plugin_audit import PluginAudit
            audit = PluginAudit(instance, probe=settings["probe"], libg=library, port=port,
                probe_sha=settings["engine_sha256"])
            before = audit.addresses()
            if variant == "plugin" and settings.get("arm_after_ready"):
                center = before["libg_base"]
                temporary = output / "arm-base.pending"
                with temporary.open("x") as arm:
                    arm.write(f"{center:016x}\n")
                temporary.rename(output / "arm-base.txt")
            elif variant == "plugin":
                check_base(center, before["libg_base"])
            else:
                center = before["libg_base"]
            arguments = [sys.executable, "-B", "-u", str(root / "plugin_probe.py"),
                "--instance", str(instance), "--probe", settings["probe"], "--libg", library,
                "--port", str(port), "--project", settings["project"], "--puct-root", settings["puct_root"],
                "--output", str(output / "report.json"), "--mode", "trace", "--center-base", str(center)]
            run_phase(output, "probe", arguments, timeout=960, environment=environment)
    finally:
        if engine is not None:
            cleanup = clean_engine(engine, instance, port, expected_identity=identity)
            write(output / "cleanup.json", cleanup)
    if cleanup is None or not cleanup["clean"]:
        raise ValueError("plugin execution owned cleanup failed")
    report = json.loads((output / "report.json").read_text())
    if variant == "plugin":
        from plugin_seal import create_seal
        seal = create_seal(output)
        opening = report["opening"]
        summary = summarize(output / "qemu.log", base=opening["libg_base"], center=center,
            reset_offset=report["reset_offset"], battle_offset=report["battle_offset"],
            final_offset=report["final_offset"], global_object=opening["libg_base"] + 0x19FC368,
            manager_object=opening["manager"] + 0xA0, seal=seal)
        write(output / "trace-summary.json", summary)
    write(output / "phase-completion.json", dict(seconds=time.monotonic() - started,
        variant=variant, training_admissible=False, gates_closed=False))
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
            raise ValueError("plugin uploaded source differs")
    started = time.monotonic()
    try:
        run(settings, "baseline")
        upgraded = run(settings, "upgraded")
        run(settings, "plugin", center=upgraded["opening"]["libg_base"])
        write(root / "completion.json", dict(status="completed_requires_independent_verification",
            settings_sha256=sha(settings_path), seconds=time.monotonic() - started,
            training_admissible=False, gates_closed=False))
    except BaseException as error:
        write(root / "failure.json", dict(error=repr(error), seconds=time.monotonic() - started,
            training_admissible=False, gates_closed=False))
        raise


if __name__ == "__main__":
    main()
